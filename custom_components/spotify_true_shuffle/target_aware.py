from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any

from .coordinator import TrueShuffleCoordinator

_LOGGER = logging.getLogger(__name__)

TARGET_CHECK_INTERVAL_SECONDS = 30


class TargetAwareTrueShuffleCoordinator(TrueShuffleCoordinator):
    """Coordinator that treats the physical TRUE SHUFFLE playlist as cycle authority.

    The source playlist remains the master only when a new cycle is created. During an
    active cycle, manual deletions and reordering in the target playlist are preserved.
    A manually deleted source track is excluded only for the current cycle and returns
    automatically when the next cycle is built from the source playlist.
    """

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry)
        self._last_target_check_monotonic = 0.0

    async def async_initialize(self) -> None:
        loaded = await self.store.async_load() or {}
        await super().async_initialize()

        same_playlists = (
            loaded.get("source_playlist_id") == self.source_id
            and loaded.get("target_playlist_id") == self.target_id
        )
        if not same_playlists:
            loaded = {}

        self.state["manual_excluded"] = [
            tid for tid in loaded.get("manual_excluded", [])
            if tid in self.state.get("source_tracks", {})
        ]
        self.state["target_snapshot"] = loaded.get("target_snapshot")
        self.state["target_external_change_pending"] = bool(
            loaded.get("target_external_change_pending", False)
        )
        self.state["target_foreign_count"] = int(loaded.get("target_foreign_count") or 0)
        self.state["pending_source_additions"] = [
            tid for tid in loaded.get("pending_source_additions", [])
            if tid in self.state.get("source_tracks", {})
        ]

    @property
    def manual_excluded_set(self) -> set[str]:
        return set(self.state.get("manual_excluded", []))

    @property
    def remaining_ids(self) -> list[str]:
        played = self.played_set
        excluded = self.manual_excluded_set
        tracks = self.state.get("source_tracks", {})
        return [
            tid for tid in self.state.get("order", [])
            if tid not in played and tid not in excluded and tid in tracks
        ]

    @property
    def snapshot(self) -> dict[str, Any]:
        data = dict(super().snapshot)
        data["manual_excluded_count"] = len(self.manual_excluded_set)
        data["target_foreign_count"] = int(self.state.get("target_foreign_count") or 0)
        return data

    @staticmethod
    def _snapshot_id(meta: dict[str, Any]) -> str | None:
        return meta.get("snapshotId") or meta.get("snapshot_id")

    async def _async_fetch_target_contents(self) -> dict[str, Any]:
        source_tracks = self.state.get("source_tracks", {})
        source_ids: list[str] = []
        source_seen: set[str] = set()
        all_ids: list[str] = []
        foreign_count = 0
        first_track_id: str | None = None
        first_track_name: str | None = None
        total = 0
        offset = 0

        while True:
            page = await self._spotify(
                "get_playlist_items",
                playlist_id=self.target_id,
                limit=50,
                offset=offset,
            )
            if offset == 0:
                total = int(page.get("total") or 0)

            items = page.get("items") or []
            for item in items:
                track = item.get("track") or {}
                track_id = track.get("id")
                if first_track_id is None and track_id:
                    first_track_id = track_id
                    first_track_name = track.get("name") or track_id
                if not track_id:
                    continue

                all_ids.append(track_id)
                if track_id in source_tracks:
                    if track_id not in source_seen:
                        source_seen.add(track_id)
                        source_ids.append(track_id)
                else:
                    foreign_count += 1

            if not page.get("next"):
                break
            offset += 50

        return {
            "total": total,
            "all_ids": all_ids,
            "source_ids": source_ids,
            "source_set": source_seen,
            "foreign_count": foreign_count,
            "first_track_id": first_track_id,
            "first_track_name": first_track_name,
        }

    def _apply_physical_target_as_cycle_authority(
        self,
        target: dict[str, Any],
        *,
        source_additions: set[str] | None = None,
    ) -> None:
        """Adopt physical target membership/order for the current cycle.

        Source additions are special: if a track was just added to the source playlist and
        has not reached the physical target yet, it is appended to the current cycle rather
        than being mistaken for a manual deletion.
        """
        tracks = self.state.get("source_tracks", {})
        played = self.played_set
        additions = set(source_additions or set())

        physical_remaining = [
            tid for tid in target.get("source_ids", [])
            if tid in tracks and tid not in played
        ]
        physical_set = set(physical_remaining)

        current_cycle_ids = [
            tid for tid in self.state.get("order", [])
            if tid in tracks and tid not in played
        ]
        current_cycle_set = set(current_cycle_ids)

        # Anything that belonged to the current cycle but is now absent from the physical
        # target is a manual exclusion, except brand-new source additions that have not yet
        # been written to the target playlist.
        excluded = {
            tid for tid in current_cycle_ids
            if tid not in physical_set and tid not in additions
        }

        # A manually re-added track automatically leaves manual_excluded because it is
        # present in physical_remaining. A source track manually inserted into the target
        # but missing from the old order is also admitted to the current cycle.
        appended_additions = [
            tid for tid in additions
            if tid in tracks and tid not in played and tid not in physical_set
        ]

        played_order = [
            tid for tid in self.state.get("order", [])
            if tid in played and tid in tracks
        ]

        # Keep excluded IDs in the tail of internal order so a later manual re-add can be
        # recognized cleanly. They are filtered out by remaining_ids for this cycle.
        excluded_tail = [tid for tid in current_cycle_ids if tid in excluded]
        new_cycle_order = physical_remaining + appended_additions

        # Include any physical source track that was not in the previous order.
        for tid in physical_remaining:
            current_cycle_set.add(tid)

        self.state["manual_excluded"] = excluded_tail
        self.state["order"] = played_order + new_cycle_order + excluded_tail
        self.state["target_total"] = int(target.get("total") or 0)
        self.state["target_foreign_count"] = int(target.get("foreign_count") or 0)
        self.state["target_first_track_id"] = target.get("first_track_id")
        self.state["target_first_track"] = target.get("first_track_name")
        self.state["target_order_ok"] = physical_remaining == self.remaining_ids

    async def _async_reconcile_target_playlist(
        self,
        *,
        meta: dict[str, Any] | None = None,
        source_additions: set[str] | None = None,
    ) -> None:
        if meta is None:
            meta = await self._spotify("get_playlist", playlist_id=self.target_id)

        target = await self._async_fetch_target_contents()
        self._apply_physical_target_as_cycle_authority(
            target,
            source_additions=source_additions,
        )
        self.state["target_snapshot"] = self._snapshot_id(meta)
        self.state["target_external_change_pending"] = False
        await self._save()

    async def _async_capture_manual_edits_before_cleanup(self) -> None:
        """Protect manual deletions before engine cleanup mutates the target playlist."""
        target = await self._async_fetch_target_contents()
        physical_set = set(target.get("source_set") or set())
        additions = set(self.state.get("pending_source_additions", []))
        tracks = self.state.get("source_tracks", {})
        played = self.played_set

        current_cycle_ids = [
            tid for tid in self.state.get("order", [])
            if tid in tracks and tid not in played
        ]
        missing = {
            tid for tid in current_cycle_ids
            if tid not in physical_set and tid not in additions
        }

        # Prevent a pending skipped-track move from re-adding a track that the user
        # manually deleted from TRUE SHUFFLE before the cleanup ran.
        self.state["pending_defer"] = [
            tid for tid in self.state.get("pending_defer", [])
            if tid not in missing
        ]

        existing = [
            tid for tid in self.state.get("manual_excluded", [])
            if tid in missing
        ]
        newly_missing = [tid for tid in current_cycle_ids if tid in missing and tid not in existing]
        self.state["manual_excluded"] = existing + newly_missing

        # For a source-change full rebuild, also preserve the actual physical order and
        # append only genuinely new source tracks. This prevents source sync from undoing
        # a manual reorder of the current cycle.
        if self.state.get("pending_full_rebuild"):
            self._apply_physical_target_as_cycle_authority(
                target,
                source_additions=additions,
            )

        await self._save()

    async def _async_check_target_changes(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_target_check_monotonic < TARGET_CHECK_INTERVAL_SECONDS:
            return
        self._last_target_check_monotonic = now

        if not self.state.get("source_tracks") or not self.state.get("order"):
            return

        meta = await self._spotify("get_playlist", playlist_id=self.target_id)
        snapshot = self._snapshot_id(meta)
        previous_snapshot = self.state.get("target_snapshot")
        changed = snapshot != previous_snapshot
        pending_external = bool(self.state.get("target_external_change_pending"))

        tracks_meta = meta.get("tracks") or {}
        self.state["target_total"] = int(tracks_meta.get("total") or self.state.get("target_total") or 0)

        if not changed and not pending_external:
            return

        # If the engine itself still has writes queued, let those finish first; the
        # post-cleanup reconciliation will then absorb both the engine changes and any
        # manual edits without fighting the live Spotify queue.
        if self.state.get("pending_target_rebuild") or not self._target_update_safe():
            self.state["target_external_change_pending"] = True
            await self._save()
            return

        await self._async_reconcile_target_playlist(meta=meta)

    async def _async_update_data(self) -> dict[str, Any]:
        await super()._async_update_data()
        try:
            await self._async_check_target_changes(force=False)
        except Exception as err:  # noqa: BLE001 - reconciliation is a recovery feature
            _LOGGER.warning("Unable to reconcile manual TRUE SHUFFLE changes: %s", err)
        return self.snapshot

    async def async_sync_source(self, force: bool = True) -> None:
        old_source_ids = set(self.state.get("source_tracks", {}))

        # If the target is currently safe and clean, absorb any manual edits before a
        # source sync has a chance to queue a rebuild.
        if (
            self.state.get("order")
            and not self.state.get("pending_target_rebuild")
            and self._target_update_safe()
        ):
            try:
                await self._async_check_target_changes(force=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Unable to pre-sync TRUE SHUFFLE target state: %s", err)

        await super().async_sync_source(force=force)

        new_source_ids = set(self.state.get("source_tracks", {}))
        additions = new_source_ids - old_source_ids
        if additions:
            self.state["pending_source_additions"] = list(additions)
        elif not self.state.get("pending_full_rebuild"):
            self.state["pending_source_additions"] = []

        # If no write was queued, a forced sync button should also immediately adopt any
        # manual target edits instead of only syncing the source playlist.
        if (
            not self.state.get("pending_target_rebuild")
            and self._target_update_safe()
        ):
            try:
                await self._async_check_target_changes(force=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Unable to post-sync TRUE SHUFFLE target state: %s", err)

        await self._save()

    async def async_apply_target_changes(self, force: bool = False) -> None:
        # Do not carry manual deletions from the previous cycle into an explicit forced
        # rebuild (new cycle / user-requested reshuffle). Normal automatic cleanup does
        # capture them before touching Spotify.
        if not force and self._target_update_safe():
            try:
                await self._async_capture_manual_edits_before_cleanup()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Unable to capture manual target edits before cleanup: %s", err)

        await super().async_apply_target_changes(force=force)

        if not self.state.get("pending_target_rebuild"):
            self.state["pending_source_additions"] = []
            try:
                await self._async_reconcile_target_playlist()
            except Exception as err:  # noqa: BLE001
                # The engine write already succeeded. Keep a pending external reconcile
                # marker instead of failing the whole update solely because verification
                # of manual edits could not complete.
                self.state["target_external_change_pending"] = True
                await self._save()
                _LOGGER.warning("Unable to reconcile target after cleanup: %s", err)

    async def async_start_new_cycle(self) -> None:
        # New cycle always starts from the full source playlist. Manual exclusions are
        # intentionally current-cycle-only and must not survive this boundary.
        self.state["manual_excluded"] = []
        self.state["target_external_change_pending"] = False
        self.state["pending_source_additions"] = []
        self.state["target_snapshot"] = None
        await self._save()
        await super().async_start_new_cycle()
