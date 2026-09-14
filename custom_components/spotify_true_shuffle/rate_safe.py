from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .coordinator import TrueShuffleCoordinator
from .relink_aware import RelinkAwareTrueShuffleCoordinator

_LOGGER = logging.getLogger(__name__)

# Conservative guard rails for a Spotify Web API quota shared with SpotifyPlus.
# Normal playback stays responsive, while an idle account no longer gets polled every
# 15 seconds around the clock.
ACTIVE_POLL_SECONDS = 30
IDLE_POLL_SECONDS = 120
BACKOFF_POLL_SECONDS = 300
TARGET_CHECK_INTERVAL_SECONDS = 600
MIN_SOURCE_SYNC_INTERVAL_MINUTES = 60
API_MIN_CALL_GAP_SECONDS = 2.0
PLAYLIST_WRITE_BATCH_SIZE = 100


class RateSafeTrueShuffleCoordinator(RelinkAwareTrueShuffleCoordinator):
    """True Shuffle coordinator with conservative Spotify API usage.

    The Spotify Web API quota is shared with SpotifyPlus itself, so True Shuffle must not
    continuously spend requests while Spotify is idle or burst dozens of requests during
    a large playlist reconciliation. This layer provides adaptive polling, a global request
    spacer, low-frequency target/source checks, and avoids full target scans unless a
    snapshot actually proves that the physical playlist changed externally.
    """

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry)
        self.update_interval = timedelta(seconds=ACTIVE_POLL_SECONDS)
        self._api_call_lock = asyncio.Lock()
        self._last_api_call_started_monotonic = 0.0
        self._spotifyplus_state_unsub = None
        self._api_calls_since_start = 0
        self._api_call_counts: dict[str, int] = {}

    @property
    def snapshot(self) -> dict[str, Any]:
        data = dict(super().snapshot)
        data["effective_poll_interval_seconds"] = int(
            self.update_interval.total_seconds() if self.update_interval else 0
        )
        data["api_min_call_gap_seconds"] = API_MIN_CALL_GAP_SECONDS
        data["target_check_interval_seconds"] = TARGET_CHECK_INTERVAL_SECONDS
        data["min_source_sync_interval_minutes"] = MIN_SOURCE_SYNC_INTERVAL_MINUTES
        data["api_calls_since_start"] = self._api_calls_since_start
        data["api_call_counts"] = dict(self._api_call_counts)
        return data

    def install_spotifyplus_state_listener(self) -> None:
        """Wake an idle coordinator when SpotifyPlus reports a meaningful state change."""
        if self._spotifyplus_state_unsub is not None:
            return

        @callback
        def _state_changed(event) -> None:
            if self._spotify_backoff_active():
                return

            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            if new_state is None:
                return

            old_value = old_state.state if old_state is not None else None
            new_value = new_state.state
            old_content = (
                old_state.attributes.get("media_content_id")
                if old_state is not None
                else None
            )
            new_content = new_state.attributes.get("media_content_id")

            # Ignore ordinary attribute/progress churn. A play/pause/idle transition or a
            # new media item is enough to wake True Shuffle immediately from its idle poll.
            if old_value == new_value and old_content == new_content:
                return

            self.update_interval = timedelta(seconds=ACTIVE_POLL_SECONDS)
            self.hass.async_create_task(self.async_request_refresh())

        self._spotifyplus_state_unsub = async_track_state_change_event(
            self.hass,
            [self.entity_id],
            _state_changed,
        )
        self.entry.async_on_unload(self._spotifyplus_state_unsub)

    async def _spotify(self, service: str, **data) -> dict[str, Any]:
        """Space every True Shuffle Spotify call so pagination/writes cannot burst the API."""
        # The parent circuit breaker must reject immediately while a Retry-After is active.
        if self._spotify_backoff_active():
            return await super()._spotify(service, **data)

        async with self._api_call_lock:
            elapsed = time.monotonic() - self._last_api_call_started_monotonic
            wait_seconds = API_MIN_CALL_GAP_SECONDS - elapsed
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            self._last_api_call_started_monotonic = time.monotonic()
            self._api_calls_since_start += 1
            self._api_call_counts[service] = self._api_call_counts.get(service, 0) + 1
            return await super()._spotify(service, **data)

    async def _async_poll_playback(self) -> None:
        await super()._async_poll_playback()

        # Keep a reasonably quick cadence only while Spotify is active, a True Shuffle
        # context is paused awaiting cleanup, or a track handoff is still being tracked.
        active = bool(
            self.state.get("current_is_playing")
            or self._target_context_active
            or self.state.get("tracking_track_id")
        )
        self.update_interval = timedelta(
            seconds=ACTIVE_POLL_SECONDS if active else IDLE_POLL_SECONDS
        )

    def _sync_due(self) -> bool:
        """Never run automatic source metadata checks more often than once per hour."""
        if self.state.get("target_total") is None or not self.state.get("source_tracks"):
            return True

        last = self._parse_iso(self.state.get("last_sync"))
        if last is None:
            return True

        configured = int(self.settings.get("sync_interval_minutes", 60) or 60)
        effective_minutes = max(MIN_SOURCE_SYNC_INTERVAL_MINUTES, configured)
        return datetime.now(timezone.utc) - last >= timedelta(minutes=effective_minutes)

    async def _async_check_target_changes(self, force: bool = False) -> None:
        """Check only target metadata periodically; scan all 2k+ items only after a change."""
        now = time.monotonic()
        if not force:
            if self._target_context_active or self.state.get("current_is_playing"):
                return
            if (
                now - self._last_target_check_monotonic
                < TARGET_CHECK_INTERVAL_SECONDS
            ):
                return

        self._last_target_check_monotonic = now

        if not self.state.get("source_tracks") or not self.state.get("order"):
            return

        meta = await self._spotify("get_playlist", playlist_id=self.target_id)
        snapshot = self._snapshot_id(meta)
        previous_snapshot = self.state.get("target_snapshot")
        changed = snapshot != previous_snapshot
        pending_external = bool(
            self.state.get("target_external_change_pending")
        )

        tracks_meta = meta.get("tracks") or {}
        self.state["target_total"] = int(
            tracks_meta.get("total") or self.state.get("target_total") or 0
        )
        self.state["target_name"] = meta.get(
            "name", self.state.get("target_name") or self.target_id
        )

        if not changed and not pending_external:
            await self._save()
            return

        if self.state.get("pending_target_rebuild") or not self._target_update_safe():
            self.state["target_external_change_pending"] = True
            await self._save()
            return

        # This is deliberately the expensive path. It runs only after the cheap snapshot
        # request proves the user (or another client) changed the physical target playlist.
        await self._async_reconcile_target_playlist(meta=meta)

    async def async_sync_source(self, force: bool = True) -> None:
        """Sync source with one cheap metadata check when unchanged.

        The previous target-aware implementation performed extra target checks before and
        after every scheduled source sync. Manual target changes already have their own
        low-frequency snapshot checker, so scheduled source checks do not need those calls.
        """
        old_source_ids = set(self.state.get("source_tracks", {}))

        # A user-pressed forced sync may explicitly absorb a known manual target edit first.
        if (
            force
            and self.state.get("order")
            and not self.state.get("pending_target_rebuild")
            and self._target_update_safe()
        ):
            try:
                await self._async_check_target_changes(force=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Unable to pre-sync TRUE SHUFFLE target state: %s", err)

        source_meta = await self._spotify("get_playlist", playlist_id=self.source_id)
        self.state["source_name"] = source_meta.get("name", self.source_id)
        source_snapshot = self._snapshot_id(source_meta)
        self.state["source_total"] = int(
            ((source_meta.get("tracks") or {}).get("total"))
            or self.state.get("source_total")
            or 0
        )

        # Target metadata is needed on first setup only; target checks and our own writes
        # maintain it afterwards.
        if self.state.get("target_name") is None or self.state.get("target_total") is None:
            target_meta = await self._spotify("get_playlist", playlist_id=self.target_id)
            self.state["target_name"] = target_meta.get("name", self.target_id)
            self.state["target_total"] = int(
                ((target_meta.get("tracks") or {}).get("total")) or 0
            )
            self.state["target_snapshot"] = self._snapshot_id(target_meta)

        if (
            not force
            and source_snapshot
            and source_snapshot == self.state.get("source_snapshot")
            and self.state.get("source_tracks")
        ):
            self.state["last_sync"] = self._utcnow_iso()
            await self._save()
            return

        tracks: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = await self._spotify(
                "get_playlist_items",
                playlist_id=self.source_id,
                limit=50,
                offset=offset,
            )
            if offset == 0:
                self.state["source_total"] = int(
                    page.get("total") or self.state.get("source_total") or 0
                )

            for item in page.get("items") or []:
                track = item.get("track") or {}
                track_id = track.get("id")
                uri = track.get("uri")
                if not track_id or not uri or not track.get("is_playable", True):
                    continue

                artists = track.get("artists") or []
                album = track.get("album") or {}
                aliases = []
                for candidate in self._track_id_candidates(track):
                    candidate_id = candidate[0]
                    if candidate_id and candidate_id != track_id and candidate_id not in aliases:
                        aliases.append(candidate_id)

                tracks.setdefault(
                    track_id,
                    {
                        "id": track_id,
                        "uri": uri,
                        "name": track.get("name") or track_id,
                        "duration_ms": int(track.get("duration_ms") or 0),
                        "artist_ids": [
                            artist.get("id")
                            for artist in artists
                            if isinstance(artist, dict) and artist.get("id")
                        ],
                        "artist_names": [
                            artist.get("name")
                            for artist in artists
                            if isinstance(artist, dict) and artist.get("name")
                        ],
                        "album_id": album.get("id"),
                        "album_name": album.get("name"),
                        "added_at": item.get("added_at"),
                        "aliases": aliases,
                    },
                )

            if not page.get("next"):
                break
            offset += 50

        old_tracks = self.state.get("source_tracks", {})
        old_ids = set(old_tracks)
        new_ids = set(tracks)
        additions = new_ids - old_ids
        removals = old_ids - new_ids

        self.state["source_tracks"] = tracks
        self.state["source_snapshot"] = source_snapshot
        self.state["last_sync"] = self._utcnow_iso()

        if self.state.get("order"):
            order = [tid for tid in self.state["order"] if tid in new_ids]
            played = [
                tid for tid in self.state.get("played", []) if tid in new_ids
            ]
            self.state["played"] = played

            if additions:
                played_set = set(played)
                remaining_tracks = [
                    tracks[tid] for tid in order if tid not in played_set
                ] + [tracks[tid] for tid in additions]
                new_remaining = self._smart_shuffle(remaining_tracks)
                played_order = [tid for tid in order if tid in played_set]
                self.state["order"] = played_order + new_remaining
                self.state["added_this_cycle"] = int(
                    self.state.get("added_this_cycle", 0)
                ) + len(additions)
            else:
                self.state["order"] = order

            if additions or removals:
                self._queue_full_rebuild()

        self.state["status"] = "running" if self.state.get("order") else "ready"

        self.state["pending_source_additions"] = list(additions) if additions else []
        await self._save()

        # A manual sync button should finish the requested work without immediately
        # scheduling one extra coordinator/API refresh afterwards.
        if force and self.state.get("pending_target_rebuild") and self._target_update_safe():
            await self.async_apply_target_changes()

        self.async_set_updated_data(self.snapshot)

    async def async_apply_target_changes(self, force: bool = False) -> None:
        """Apply writes without scanning the entire target playlist on every cleanup."""
        if not force and self._target_update_safe():
            try:
                # One cheap snapshot request decides whether the expensive full target scan
                # is needed to preserve a manual deletion/reorder before our own write.
                meta = await self._spotify("get_playlist", playlist_id=self.target_id)
                current_snapshot = self._snapshot_id(meta)
                stored_snapshot = self.state.get("target_snapshot")
                external_changed = (
                    stored_snapshot is None
                    or current_snapshot != stored_snapshot
                    or bool(self.state.get("target_external_change_pending"))
                )
                if external_changed:
                    await self._async_capture_manual_edits_before_cleanup()
                    self.state["target_snapshot"] = current_snapshot
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Unable to check manual target edits before cleanup: %s", err)

        # Bypass TargetAware's unconditional post-write full reconciliation. The base
        # method performs the requested write plus a one-item order verification.
        await TrueShuffleCoordinator.async_apply_target_changes(self, force=force)

        if self.state.get("pending_target_rebuild"):
            return

        self.state["pending_source_additions"] = []
        try:
            meta = await self._spotify("get_playlist", playlist_id=self.target_id)
            self.state["target_snapshot"] = self._snapshot_id(meta)
            self.state["target_name"] = meta.get(
                "name", self.state.get("target_name") or self.target_id
            )
            self.state["target_total"] = int(
                ((meta.get("tracks") or {}).get("total"))
                or self.state.get("target_total")
                or 0
            )
            self.state["target_external_change_pending"] = False
        except Exception as err:  # noqa: BLE001
            # The write already succeeded. Do not repeat it just because the cheap
            # metadata refresh failed; a later target check can reconcile the snapshot.
            self.state["target_external_change_pending"] = True
            _LOGGER.warning("Unable to refresh target snapshot after cleanup: %s", err)
        await self._save()

    async def _async_full_rebuild_locked(self) -> None:
        """Use Spotify's supported 100-item write batch to halve rebuild requests."""
        uris = [
            self.state["source_tracks"][tid]["uri"] for tid in self.remaining_ids
        ]
        await self._spotify("playlist_items_clear", playlist_id=self.target_id)
        for start in range(0, len(uris), PLAYLIST_WRITE_BATCH_SIZE):
            await self._spotify(
                "playlist_items_add",
                playlist_id=self.target_id,
                uris=",".join(uris[start : start + PLAYLIST_WRITE_BATCH_SIZE]),
            )
        self.state["pending_full_rebuild"] = False
        self.state["pending_remove"] = []
        self.state["pending_defer"] = []
        self.state["target_total"] = len(uris)
