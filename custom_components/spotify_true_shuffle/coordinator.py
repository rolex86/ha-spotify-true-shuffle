from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import logging
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ALBUM_GAP, CONF_ARTIST_GAP, CONF_AUTO_NEW_CYCLE, CONF_CONTEXT_ONLY,
    CONF_MIN_PERCENT, CONF_MIN_SECONDS, CONF_POLL_INTERVAL, CONF_SOURCE_PLAYLIST,
    CONF_SPOTIFYPLUS_ENTITY, CONF_SYNC_INTERVAL, CONF_TARGET_PLAYLIST,
    DEFAULT_ALBUM_GAP, DEFAULT_ARTIST_GAP, DEFAULT_AUTO_NEW_CYCLE,
    DEFAULT_CONTEXT_ONLY, DEFAULT_MIN_PERCENT, DEFAULT_MIN_SECONDS,
    DEFAULT_POLL_INTERVAL, DEFAULT_SYNC_INTERVAL, DOMAIN, SPOTIFYPLUS_DOMAIN,
    STORE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Spotify Connect can briefly report an empty / different context while handing playback
# between devices (for example phone -> car).  Keep the last True Shuffle track around
# for a short grace period so we do not lose the transition or edit the playlist mid-handoff.
CONTEXT_GRACE_SECONDS = 45


class TrueShuffleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.settings = {**entry.data, **entry.options}
        poll = int(self.settings.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        super().__init__(hass, _LOGGER, name=f"Spotify True Shuffle {entry.entry_id}", update_interval=timedelta(seconds=poll))
        self.store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.state: dict[str, Any] = {}
        self._last_track_seen: str | None = None
        self._target_context_active = False
        self._target_lock = asyncio.Lock()

    @property
    def entity_id(self) -> str:
        return self.settings[CONF_SPOTIFYPLUS_ENTITY]

    @property
    def source_id(self) -> str:
        return self.settings[CONF_SOURCE_PLAYLIST]

    @property
    def target_id(self) -> str:
        return self.settings[CONF_TARGET_PLAYLIST]

    async def async_initialize(self) -> None:
        loaded = await self.store.async_load() or {}
        if loaded.get("source_playlist_id") != self.source_id or loaded.get("target_playlist_id") != self.target_id:
            loaded = {}

        pending_remove = loaded.get("pending_remove", [])
        pending_defer = loaded.get("pending_defer", [])
        legacy_pending = bool(loaded.get("pending_target_rebuild", False))
        pending_full_rebuild = bool(
            loaded.get("pending_full_rebuild", legacy_pending and not pending_remove and not pending_defer)
        )

        self.state = {
            "source_playlist_id": self.source_id,
            "target_playlist_id": self.target_id,
            "source_name": loaded.get("source_name"),
            "target_name": loaded.get("target_name"),
            "source_snapshot": loaded.get("source_snapshot"),
            "source_total": loaded.get("source_total", 0),
            "target_total": loaded.get("target_total"),
            "source_tracks": loaded.get("source_tracks", {}),
            "cycle": loaded.get("cycle", 0),
            "order": loaded.get("order", []),
            "played": loaded.get("played", []),
            "skipped": loaded.get("skipped", 0),
            "added_this_cycle": loaded.get("added_this_cycle", 0),
            "cycle_started": loaded.get("cycle_started"),
            "last_sync": loaded.get("last_sync"),
            "pending_remove": pending_remove,
            "pending_defer": pending_defer,
            "pending_full_rebuild": pending_full_rebuild,
            "pending_target_rebuild": legacy_pending or bool(pending_remove or pending_defer or pending_full_rebuild),
            "target_first_track": loaded.get("target_first_track"),
            "target_first_track_id": loaded.get("target_first_track_id"),
            "target_order_ok": loaded.get("target_order_ok"),
            "status": loaded.get("status", "loading"),
            # Persistent playback tracker.  These fields deliberately do not use the
            # current_ prefix so they survive Home Assistant restarts and Connect handoffs.
            "tracking_track_id": loaded.get("tracking_track_id"),
            "tracking_progress_ms": int(loaded.get("tracking_progress_ms") or 0),
            "tracking_duration_ms": int(loaded.get("tracking_duration_ms") or 0),
            "tracking_last_seen": loaded.get("tracking_last_seen"),
            "tracking_context_lost_at": loaded.get("tracking_context_lost_at"),
            # Live UI-only playback data.
            "current_track": None,
            "current_artist": None,
            "current_track_id": None,
            "current_context": None,
            "current_progress_ms": 0,
        }

        self._last_track_seen = self.state.get("tracking_track_id")
        if self._last_track_seen and self._last_track_seen in self.played_set:
            self._clear_tracking()

    async def _save(self) -> None:
        await self.store.async_save({k: v for k, v in self.state.items() if not k.startswith("current_")})

    async def _spotify(self, service: str, **data) -> dict[str, Any]:
        if not self.hass.services.has_service(SPOTIFYPLUS_DOMAIN, service):
            raise UpdateFailed(f"SpotifyPlus service {service} is not available")
        response = await self.hass.services.async_call(
            SPOTIFYPLUS_DOMAIN, service, {"entity_id": self.entity_id, **data},
            blocking=True, return_response=True,
        )
        if not isinstance(response, dict) or "result" not in response:
            raise UpdateFailed(f"SpotifyPlus service {service} returned an invalid response")
        result = response.get("result")
        return result if isinstance(result, dict) else {"value": result}

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _tracking_threshold_ms(self, duration_ms: int) -> int:
        min_seconds = int(self.settings.get(CONF_MIN_SECONDS, DEFAULT_MIN_SECONDS)) * 1000
        min_percent = int(self.settings.get(CONF_MIN_PERCENT, DEFAULT_MIN_PERCENT))
        percent_threshold = int(duration_ms * (min_percent / 100.0)) if duration_ms else 0
        return min(min_seconds, percent_threshold) if min_seconds and percent_threshold else max(min_seconds, percent_threshold)

    def _clear_tracking(self) -> None:
        self.state["tracking_track_id"] = None
        self.state["tracking_progress_ms"] = 0
        self.state["tracking_duration_ms"] = 0
        self.state["tracking_last_seen"] = None
        self.state["tracking_context_lost_at"] = None
        self._last_track_seen = None

    def _mark_played(self, track_id: str) -> bool:
        if track_id not in self.state.get("source_tracks", {}) or track_id in self.played_set:
            return False
        self.state["played"] = [*self.state.get("played", []), track_id]
        self._queue_played_removal(track_id)
        self.state["status"] = "complete" if self.remaining_count == 0 else "running"
        return True

    def _finalize_tracked_track(self, count_skip: bool) -> bool:
        """Finalize the remembered True Shuffle track before clearing the tracker."""
        track_id = self.state.get("tracking_track_id")
        if not track_id:
            return False

        changed = False
        if track_id in self.state.get("source_tracks", {}) and track_id not in self.played_set:
            progress = int(self.state.get("tracking_progress_ms") or 0)
            duration = int(self.state.get("tracking_duration_ms") or 0)
            threshold = self._tracking_threshold_ms(duration)

            if progress >= threshold:
                changed = self._mark_played(track_id) or changed
            elif count_skip:
                self.state["skipped"] = int(self.state.get("skipped", 0)) + 1
                self._defer_skipped_track(track_id)
                changed = True

        self._clear_tracking()
        return True or changed

    def _target_update_safe(self) -> bool:
        """Return True when it is safe to mutate the physical target playlist."""
        if self._target_context_active:
            return False

        tracked = self.state.get("tracking_track_id")
        if not tracked:
            return True

        lost_at = self._parse_iso(self.state.get("tracking_context_lost_at"))
        if lost_at is None:
            return False

        return datetime.now(timezone.utc) - lost_at >= timedelta(seconds=CONTEXT_GRACE_SECONDS)

    def _sync_due(self) -> bool:
        if self.state.get("target_total") is None:
            return True
        last = self.state.get("last_sync")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        mins = int(self.settings.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL))
        return datetime.now(timezone.utc) - last_dt >= timedelta(minutes=mins)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if self._sync_due():
                await self.async_sync_source(force=False)
            await self._async_poll_playback()
            if self.state.get("pending_target_rebuild") and self._target_update_safe():
                await self.async_apply_target_changes()
            if self.remaining_count == 0 and self.state.get("order") and self.settings.get(CONF_AUTO_NEW_CYCLE, DEFAULT_AUTO_NEW_CYCLE) and self._target_update_safe():
                await self.async_start_new_cycle()
            return self.snapshot
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    @property
    def played_set(self) -> set[str]:
        return set(self.state.get("played", []))

    @property
    def remaining_ids(self) -> list[str]:
        played = self.played_set
        tracks = self.state.get("source_tracks", {})
        return [tid for tid in self.state.get("order", []) if tid not in played and tid in tracks]

    @property
    def remaining_count(self) -> int:
        return len(self.remaining_ids)

    @property
    def expected_first_track_id(self) -> str | None:
        remaining = self.remaining_ids
        return remaining[0] if remaining else None

    @property
    def expected_first_track(self) -> str | None:
        tid = self.expected_first_track_id
        return self.state.get("source_tracks", {}).get(tid, {}).get("name") if tid else None

    @property
    def snapshot(self) -> dict[str, Any]:
        unique = len(self.state.get("source_tracks", {}))
        played = len(self.played_set)
        remaining = self.remaining_count
        cycle_total = played + remaining
        progress = round((played / cycle_total) * 100, 1) if cycle_total else 0.0
        return {
            **self.state,
            "source_unique": unique,
            "source_duplicates": max(0, int(self.state.get("source_total", 0)) - unique),
            "played_count": played,
            "remaining_count": remaining,
            "progress_percent": progress,
            "expected_first_track": self.expected_first_track,
            "expected_first_track_id": self.expected_first_track_id,
        }

    async def async_sync_source(self, force: bool = True) -> None:
        source_meta = await self._spotify("get_playlist", playlist_id=self.source_id)
        target_meta = await self._spotify("get_playlist", playlist_id=self.target_id)
        self.state["source_name"] = source_meta.get("name", self.source_id)
        self.state["target_name"] = target_meta.get("name", self.target_id)
        self.state["target_total"] = int(((target_meta.get("tracks") or {}).get("total")) or 0)
        snapshot = source_meta.get("snapshotId") or source_meta.get("snapshot_id")
        self.state["source_total"] = int(((source_meta.get("tracks") or {}).get("total")) or 0)

        if not force and snapshot and snapshot == self.state.get("source_snapshot") and self.state.get("source_tracks"):
            self.state["last_sync"] = self._utcnow_iso()
            await self._verify_target_order()
            await self._save()
            return

        tracks: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = await self._spotify("get_playlist_items", playlist_id=self.source_id, limit=50, offset=offset)
            if offset == 0:
                self.state["source_total"] = int(page.get("total") or self.state.get("source_total") or 0)
            for item in page.get("items") or []:
                track = item.get("track") or {}
                track_id = track.get("id")
                uri = track.get("uri")
                if not track_id or not uri or not track.get("is_playable", True):
                    continue
                artists = track.get("artists") or []
                album = track.get("album") or {}
                tracks.setdefault(track_id, {
                    "id": track_id,
                    "uri": uri,
                    "name": track.get("name") or track_id,
                    "duration_ms": int(track.get("duration_ms") or 0),
                    "artist_ids": [a.get("id") for a in artists if a.get("id")],
                    "artist_names": [a.get("name") for a in artists if a.get("name")],
                    "album_id": album.get("id"),
                    "album_name": album.get("name"),
                    "added_at": item.get("added_at"),
                })
            if not page.get("next"):
                break
            offset += 50

        old_tracks = self.state.get("source_tracks", {})
        old_ids = set(old_tracks)
        new_ids = set(tracks)
        additions = new_ids - old_ids
        removals = old_ids - new_ids
        self.state["source_tracks"] = tracks
        self.state["source_snapshot"] = snapshot
        self.state["last_sync"] = self._utcnow_iso()

        if self.state.get("order"):
            order = [tid for tid in self.state["order"] if tid in new_ids]
            played = [tid for tid in self.state.get("played", []) if tid in new_ids]
            self.state["played"] = played
            if additions:
                played_set = set(played)
                remaining_tracks = [tracks[tid] for tid in order if tid not in played_set] + [tracks[tid] for tid in additions]
                new_remaining = self._smart_shuffle(remaining_tracks)
                played_order = [tid for tid in order if tid in played_set]
                self.state["order"] = played_order + new_remaining
                self.state["added_this_cycle"] = int(self.state.get("added_this_cycle", 0)) + len(additions)
            else:
                self.state["order"] = order
            if additions or removals:
                self._queue_full_rebuild()
        self.state["status"] = "running" if self.state.get("order") else "ready"
        if not self.state.get("pending_target_rebuild"):
            await self._verify_target_order()
        await self._save()

    def _smart_shuffle(self, tracks: list[dict[str, Any]]) -> list[str]:
        remaining = list(tracks)
        random.SystemRandom().shuffle(remaining)
        result: list[str] = []
        artist_gap = int(self.settings.get(CONF_ARTIST_GAP, DEFAULT_ARTIST_GAP))
        album_gap = int(self.settings.get(CONF_ALBUM_GAP, DEFAULT_ALBUM_GAP))
        artist_history: deque[set[str]] = deque(maxlen=max(1, artist_gap))
        album_history: deque[str | None] = deque(maxlen=max(1, album_gap))
        rng = random.SystemRandom()

        while remaining:
            choice_index = None
            for a_gap in range(artist_gap, -1, -1):
                album_ranges = range(album_gap, -1, -1) if a_gap == artist_gap else (0,)
                for al_gap in album_ranges:
                    recent_artists = set().union(*list(artist_history)[-a_gap:]) if a_gap and artist_history else set()
                    recent_albums = set(list(album_history)[-al_gap:]) if al_gap and album_history else set()
                    candidates = [
                        i for i, track in enumerate(remaining)
                        if not (set(track.get("artist_ids", [])) & recent_artists)
                        and (not track.get("album_id") or track.get("album_id") not in recent_albums)
                    ]
                    if candidates:
                        choice_index = rng.choice(candidates)
                        break
                if choice_index is not None:
                    break
            if choice_index is None:
                choice_index = rng.randrange(len(remaining))
            track = remaining.pop(choice_index)
            result.append(track["id"])
            if artist_gap:
                artist_history.append(set(track.get("artist_ids", [])))
            if album_gap:
                album_history.append(track.get("album_id"))
        return result

    def _queue_full_rebuild(self) -> None:
        self.state["pending_full_rebuild"] = True
        self.state["pending_remove"] = []
        self.state["pending_defer"] = []
        self.state["pending_target_rebuild"] = True

    def _queue_played_removal(self, track_id: str) -> None:
        pending_defer = [tid for tid in self.state.get("pending_defer", []) if tid != track_id]
        pending_remove = list(self.state.get("pending_remove", []))
        if track_id not in pending_remove:
            pending_remove.append(track_id)
        self.state["pending_defer"] = pending_defer
        self.state["pending_remove"] = pending_remove
        self.state["pending_target_rebuild"] = True

    def _defer_skipped_track(self, track_id: str) -> bool:
        """Move an unplayed skipped track to the end of the current cycle."""
        remaining = self.remaining_ids
        if track_id not in remaining or not remaining or remaining[-1] == track_id:
            return False
        order = list(self.state.get("order", []))
        try:
            order.remove(track_id)
        except ValueError:
            return False
        order.append(track_id)
        self.state["order"] = order
        if track_id not in self.state.get("pending_remove", []):
            pending_defer = list(self.state.get("pending_defer", []))
            if track_id not in pending_defer:
                pending_defer.append(track_id)
            self.state["pending_defer"] = pending_defer
        self.state["pending_target_rebuild"] = True
        return True

    async def async_start_new_cycle(self) -> None:
        tracks = list(self.state.get("source_tracks", {}).values())
        if not tracks:
            await self.async_sync_source(force=True)
            tracks = list(self.state.get("source_tracks", {}).values())
        if not tracks:
            raise UpdateFailed("Source playlist has no playable tracks")
        self.state["cycle"] = int(self.state.get("cycle", 0)) + 1
        self.state["order"] = self._smart_shuffle(tracks)
        self.state["played"] = []
        self.state["skipped"] = 0
        self.state["added_this_cycle"] = 0
        self.state["cycle_started"] = self._utcnow_iso()
        self.state["status"] = "building"
        self._clear_tracking()
        self._queue_full_rebuild()
        await self._save()
        await self.async_apply_target_changes(force=True)
        self.async_set_updated_data(self.snapshot)

    async def async_rebuild_target_remaining(self, force: bool = False) -> None:
        """Compatibility wrapper for callers that explicitly request a full rebuild."""
        self._queue_full_rebuild()
        await self.async_apply_target_changes(force=force)

    async def async_apply_target_changes(self, force: bool = False) -> None:
        async with self._target_lock:
            if not force and not self.state.get("pending_target_rebuild"):
                return
            if not force and not self._target_update_safe():
                self.state["pending_target_rebuild"] = True
                await self._save()
                return

            self.state["status"] = "updating"
            await self._save()

            try:
                if self.state.get("pending_full_rebuild"):
                    await self._async_full_rebuild_locked()
                else:
                    await self._async_incremental_update_locked()
                await self._verify_target_order()
            except Exception:
                self.state["pending_target_rebuild"] = True
                self.state["status"] = "update_pending"
                await self._save()
                raise

            self.state["pending_target_rebuild"] = False
            self.state["status"] = "complete" if not self.remaining_ids and self.state.get("order") else ("running" if self.state.get("order") else "ready")
            await self._save()

    async def _async_full_rebuild_locked(self) -> None:
        uris = [self.state["source_tracks"][tid]["uri"] for tid in self.remaining_ids]
        await self._spotify("playlist_items_clear", playlist_id=self.target_id)
        for start in range(0, len(uris), 50):
            await self._spotify(
                "playlist_items_add",
                playlist_id=self.target_id,
                uris=",".join(uris[start:start + 50]),
            )
        self.state["pending_full_rebuild"] = False
        self.state["pending_remove"] = []
        self.state["pending_defer"] = []
        self.state["target_total"] = len(uris)

    async def _async_incremental_update_locked(self) -> None:
        tracks = self.state.get("source_tracks", {})
        pending_remove = list(dict.fromkeys(self.state.get("pending_remove", [])))
        pending_defer = [tid for tid in dict.fromkeys(self.state.get("pending_defer", [])) if tid not in pending_remove]

        remove_uris = [tracks[tid]["uri"] for tid in pending_remove if tid in tracks]
        for start in range(0, len(remove_uris), 100):
            await self._spotify(
                "playlist_items_remove",
                playlist_id=self.target_id,
                uris=",".join(remove_uris[start:start + 100]),
            )

        for tid in pending_defer:
            track = tracks.get(tid)
            if not track or tid in self.played_set:
                continue
            uri = track["uri"]
            await self._spotify("playlist_items_remove", playlist_id=self.target_id, uris=uri)
            await self._spotify("playlist_items_add", playlist_id=self.target_id, uris=uri)

        self.state["pending_remove"] = []
        self.state["pending_defer"] = []
        self.state["target_total"] = self.remaining_count

    async def _verify_target_order(self) -> None:
        page = await self._spotify("get_playlist_items", playlist_id=self.target_id, limit=1, offset=0)
        self.state["target_total"] = int(page.get("total") or 0)
        items = page.get("items") or []
        track = (items[0].get("track") or {}) if items else {}
        actual_id = track.get("id")
        actual_name = track.get("name")
        expected_id = self.expected_first_track_id
        self.state["target_first_track_id"] = actual_id
        self.state["target_first_track"] = actual_name
        self.state["target_order_ok"] = actual_id == expected_id

    async def async_reshuffle_remaining(self) -> None:
        if not self._target_update_safe():
            raise UpdateFailed("Cannot reshuffle while the True Shuffle playlist is active or Spotify Connect is handing playback between devices")
        remaining_tracks = [self.state["source_tracks"][tid] for tid in self.remaining_ids]
        played_order = [tid for tid in self.state.get("order", []) if tid in self.played_set]
        self.state["order"] = played_order + self._smart_shuffle(remaining_tracks)
        self._queue_full_rebuild()
        await self.async_apply_target_changes(force=True)
        self.async_set_updated_data(self.snapshot)

    async def _async_poll_playback(self) -> None:
        playback = await self._spotify("get_player_playback_state")
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        if playback.get("is_empty"):
            self._target_context_active = False
            changed = False
            if self.state.get("tracking_track_id"):
                lost_at = self._parse_iso(self.state.get("tracking_context_lost_at"))
                if lost_at is None:
                    self.state["tracking_context_lost_at"] = now_iso
                    changed = True
                elif now - lost_at >= timedelta(seconds=CONTEXT_GRACE_SECONDS):
                    changed = self._finalize_tracked_track(count_skip=True) or changed
            if changed:
                await self._save()
            return

        context = playback.get("context") or {}
        context_uri = context.get("uri")
        target_uri = f"spotify:playlist:{self.target_id}"
        context_matches = context_uri == target_uri
        self._target_context_active = context_matches

        item = playback.get("item") or {}
        track_id = item.get("id")
        progress = int(playback.get("progress_ms") or 0)
        duration = int(item.get("duration_ms") or 0)

        self.state["current_track"] = item.get("name")
        artists = item.get("artists") or []
        self.state["current_artist"] = ", ".join(a.get("name") for a in artists if a.get("name")) or None
        self.state["current_track_id"] = track_id
        self.state["current_context"] = context_uri
        self.state["current_progress_ms"] = progress

        context_only = bool(self.settings.get(CONF_CONTEXT_ONLY, DEFAULT_CONTEXT_ONLY))
        eligible_context = context_matches or not context_only
        changed = False

        if eligible_context and track_id:
            # A valid target context returned during the grace period: cancel the handoff timer.
            if self.state.get("tracking_context_lost_at") is not None:
                self.state["tracking_context_lost_at"] = None
                changed = True

            tracked_id = self.state.get("tracking_track_id")
            if tracked_id and tracked_id != track_id:
                # We finally saw the next track.  Close the remembered one instead of
                # blindly forgetting it when Spotify Connect briefly changed context.
                changed = self._finalize_tracked_track(count_skip=True) or changed

            if track_id in self.state.get("source_tracks", {}):
                source_track = self.state["source_tracks"][track_id]
                duration = int(duration or source_track.get("duration_ms") or 0)

                if self.state.get("tracking_track_id") != track_id:
                    self.state["tracking_track_id"] = track_id
                    self.state["tracking_progress_ms"] = progress
                    self.state["tracking_duration_ms"] = duration
                    self.state["tracking_last_seen"] = now_iso
                    self._last_track_seen = track_id
                    changed = True
                else:
                    if progress > int(self.state.get("tracking_progress_ms") or 0):
                        self.state["tracking_progress_ms"] = progress
                        changed = True
                    if duration and duration != int(self.state.get("tracking_duration_ms") or 0):
                        self.state["tracking_duration_ms"] = duration
                        changed = True
                    self.state["tracking_last_seen"] = now_iso
                    self._last_track_seen = track_id

                if track_id not in self.played_set:
                    tracked_progress = int(self.state.get("tracking_progress_ms") or 0)
                    threshold = self._tracking_threshold_ms(duration)
                    if tracked_progress >= threshold:
                        changed = self._mark_played(track_id) or changed

        else:
            # Do not throw away the last True Shuffle track immediately.  Spotify Connect
            # often reports another / empty context for a few polls during a device handoff.
            if self.state.get("tracking_track_id"):
                lost_at = self._parse_iso(self.state.get("tracking_context_lost_at"))
                if lost_at is None:
                    self.state["tracking_context_lost_at"] = now_iso
                    changed = True
                elif now - lost_at >= timedelta(seconds=CONTEXT_GRACE_SECONDS):
                    changed = self._finalize_tracked_track(count_skip=True) or changed

        if changed:
            await self._save()
