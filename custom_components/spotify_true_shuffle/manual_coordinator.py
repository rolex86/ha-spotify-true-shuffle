from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .coordinator import TrueShuffleCoordinator


class ManualAwareTrueShuffleCoordinator(TrueShuffleCoordinator):
    """True Shuffle coordinator that treats the physical target as current-cycle truth.

    The source playlist remains the master catalogue for a new cycle. During an active
    cycle, however, manual deletions and reordering in the physical TRUE SHUFFLE playlist
    are intentional user choices and must be preserved.
    """

    async def async_initialize(self) -> None:
        stored = await self.store.async_load() or {}
        await super().async_initialize()

        self.state["manual_excluded"] = [
            tid
            for tid in stored.get("manual_excluded", [])
            if tid in self.state.get("source_tracks", {})
        ]
        self.state["target_snapshot"] = stored.get("target_snapshot")
        self.state["target_unmanaged_count"] = int(stored.get("target_unmanaged_count") or 0)
        self.state["last_target_reconcile"] = stored.get("last_target_reconcile")

    @property
    def remaining_ids(self) -> list[str]:
        played = self.played_set
        excluded = set(self.state.get("manual_excluded", []))
        tracks = self.state.get("source_tracks", {})
        return [
            tid
            for tid in self.state.get("order", [])
            if tid not in played and tid not in excluded and tid in tracks
        ]

    @property
    def snapshot(self) -> dict[str, Any]:
        data = super().snapshot
        data["manual_excluded_count"] = len(set(self.state.get("manual_excluded", [])))
        data["target_unmanaged_count"] = int(self.state.get("target_unmanaged_count") or 0)
        data["last_target_reconcile"] = self.state.get("last_target_reconcile")
        return data

    @staticmethod
    def _playlist_snapshot(meta: dict[str, Any]) -> str | None:
        return meta.get("snapshotId") or meta.get("snapshot_id")

    async def _async_fetch_target_items(self) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        offset = 0
        total = 0

        while True:
            page = await self._spotify(
                "get_playlist_items",
                playlist_id=self.target_id,
                limit=50,
                offset=offset,
            )
            if offset == 0:
                total = int(page.get("total") or 0)

            page_items = page.get("items") or []
            for item in page_items:
                track = item.get("track") or {}
                if track.get("id"):
                    items.append(track)

            if not page.get("next"):
                break
            offset += 50

        return items, total

    async def _async_reconcile_target_if_changed(
        self,
        *,
        force: bool = False,
        target_meta: dict[str, Any] | None = None,
    ) -> bool:
        """Adopt manual target deletions / reordering into the current cycle.

        Source tracks manually removed from TRUE SHUFFLE are excluded only from the
        current cycle. A new cycle clears that exclusion and rebuilds from the source.
        Tracks manually re-added are automatically re-enabled. Foreign tracks may stay
        physically in the target, but the engine ignores them.
        """
        meta = target_meta or await self._spotify("get_playlist", playlist_id=self.target_id)
        snapshot = self._playlist_snapshot(meta)
        total_from_meta = int(((meta.get("tracks") or {}).get("total")) or 0)

        if (
            not force
            and snapshot
            and snapshot == self.state.get("target_snapshot")
        ):
            self.state["target_total"] = total_from_meta
            return False

        physical_tracks, physical_total = await self._async_fetch_target_items()
        source_tracks = self.state.get("source_tracks", {})
        source_ids = set(source_tracks)
        played = self.played_set

        physical_ids = [track.get("id") for track in physical_tracks if track.get("id")]
        physical_id_set = set(physical_ids)

        # Build the physical sequence of source tracks that are still eligible in this
        # cycle. Duplicates, foreign tracks and already-played tracks are intentionally
        # ignored by the engine while remaining untouched in Spotify.
        managed_sequence: list[str] = []
        managed_seen: set[str] = set()
        for track_id in physical_ids:
            if track_id not in source_ids or track_id in played or track_id in managed_seen:
                continue
            managed_seen.add(track_id)
            managed_sequence.append(track_id)

        old_excluded = set(self.state.get("manual_excluded", [])) & source_ids
        current_cycle_ids = {
            tid
            for tid in self.state.get("order", [])
            if tid in source_ids and tid not in played
        }

        # Missing current-cycle source tracks were manually removed from TRUE SHUFFLE.
        # If an excluded track is manually put back, presence in the target re-enables it.
        missing_now = current_cycle_ids - physical_id_set
        new_excluded = (old_excluded | missing_now) - physical_id_set
        self.state["manual_excluded"] = list(new_excluded)

        # Spotify's physical order becomes the order for the remaining current cycle.
        # Played entries stay at the front only as bookkeeping; remaining_ids filters them.
        played_order = [
            tid
            for tid in self.state.get("order", [])
            if tid in played and tid in source_ids
        ]
        self.state["order"] = played_order + managed_sequence

        # If a manually deleted track was also waiting to be deferred, do not add it back.
        self.state["pending_defer"] = [
            tid
            for tid in self.state.get("pending_defer", [])
            if tid not in new_excluded and tid in physical_id_set
        ]

        # A played track manually removed before our cleanup no longer needs an API remove.
        self.state["pending_remove"] = [
            tid
            for tid in self.state.get("pending_remove", [])
            if tid in physical_id_set
        ]

        if not self.state.get("pending_full_rebuild"):
            self.state["pending_target_rebuild"] = bool(
                self.state.get("pending_remove") or self.state.get("pending_defer")
            )
            if not self.state["pending_target_rebuild"] and self.state.get("order"):
                self.state["status"] = "running"

        managed_occurrences = len(managed_sequence)
        self.state["target_unmanaged_count"] = max(0, physical_total - managed_occurrences)
        self.state["target_total"] = physical_total
        self.state["target_snapshot"] = snapshot
        self.state["last_target_reconcile"] = datetime.now(timezone.utc).isoformat()

        first = physical_tracks[0] if physical_tracks else {}
        self.state["target_first_track_id"] = first.get("id")
        self.state["target_first_track"] = first.get("name")

        # Known source-track order is now adopted from Spotify. Unmanaged physical items
        # are reported separately and do not make the current-cycle order invalid.
        self.state["target_order_ok"] = managed_sequence == self.remaining_ids

        await self._save()
        return True

    async def _async_refresh_target_snapshot(self) -> None:
        meta = await self._spotify("get_playlist", playlist_id=self.target_id)
        self.state["target_snapshot"] = self._playlist_snapshot(meta)
        self.state["target_total"] = int(((meta.get("tracks") or {}).get("total")) or 0)
        self.state["last_target_reconcile"] = datetime.now(timezone.utc).isoformat()
        await self._save()

    async def async_sync_source(self, force: bool = True) -> None:
        # First adopt any manual target changes against the OLD source catalogue. This is
        # important when the source itself also changed since the last sync.
        try:
            await self._async_reconcile_target_if_changed(force=False)
        except Exception:
            # Source sync must still be able to repair itself if target inspection fails.
            pass

        old_source_ids = set(self.state.get("source_tracks", {}))
        physical_remaining_order = list(self.remaining_ids)

        await super().async_sync_source(force=force)

        new_source_ids = set(self.state.get("source_tracks", {}))
        additions = new_source_ids - old_source_ids
        removals = old_source_ids - new_source_ids

        # Keep current-cycle manual exclusions only for tracks that still exist in source.
        self.state["manual_excluded"] = [
            tid
            for tid in self.state.get("manual_excluded", [])
            if tid in new_source_ids
        ]

        if additions or removals:
            played = self.played_set
            excluded = set(self.state.get("manual_excluded", []))

            # Preserve the physical target order for surviving current-cycle tracks.
            survivors = [
                tid
                for tid in physical_remaining_order
                if tid in new_source_ids and tid not in played and tid not in excluded
            ]

            # New source tracks join this cycle after the existing physical queue without
            # reshuffling what the user already sees in Spotify.
            addition_tracks = [
                self.state["source_tracks"][tid]
                for tid in additions
                if tid not in played and tid not in excluded
            ]
            addition_order = self._smart_shuffle(addition_tracks) if addition_tracks else []

            played_order = [
                tid
                for tid in self.state.get("order", [])
                if tid in played and tid in new_source_ids
            ]
            self.state["order"] = played_order + survivors + addition_order

            # Source changes still require a physical rebuild, but manual deletions and
            # manual ordering of surviving source tracks are preserved by the order above.
            self._queue_full_rebuild()

        await self._save()

    async def async_apply_target_changes(self, force: bool = False) -> None:
        # Before an automatic cleanup, adopt any manual changes made since the last sync.
        # Forced operations (new cycle / explicit reshuffle) intentionally remain engine-led.
        if not force:
            try:
                await self._async_reconcile_target_if_changed(force=False)
            except Exception:
                pass

        await super().async_apply_target_changes(force=force)
        await self._async_refresh_target_snapshot()

    async def async_start_new_cycle(self) -> None:
        # A new cycle is always rebuilt from the complete source catalogue. Manual target
        # deletions are therefore intentionally forgotten here and can appear again.
        self.state["manual_excluded"] = []
        self.state["target_unmanaged_count"] = 0
        self.state["target_snapshot"] = None
        await super().async_start_new_cycle()
