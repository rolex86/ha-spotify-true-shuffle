from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import CONF_AUTO_NEW_CYCLE, DEFAULT_AUTO_NEW_CYCLE

_LOGGER = logging.getLogger(__name__)

TARGET_RECONCILE_INTERVAL_SECONDS = 60


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


def _snapshot_id(meta: dict[str, Any]) -> str | None:
    return meta.get("snapshotId") or meta.get("snapshot_id")


async def _fetch_target_items(coordinator) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    offset = 0
    total = 0
    while True:
        page = await coordinator._spotify(
            "get_playlist_items",
            playlist_id=coordinator.target_id,
            limit=50,
            offset=offset,
        )
        if offset == 0:
            total = int(page.get("total") or 0)
        items.extend(item for item in (page.get("items") or []) if isinstance(item, dict))
        if not page.get("next"):
            break
        offset += 50
    return items, total


async def _reconcile_target(coordinator, *, force: bool = False, target_meta: dict[str, Any] | None = None) -> bool:
    """Adopt manual changes in TRUE SHUFFLE for the current cycle.

    The source playlist remains the master list for a new cycle. During an active cycle,
    however, a manual removal from TRUE SHUFFLE means "do not play again this cycle",
    and a manual reorder becomes the current physical order. Tracks not present in the
    source are preserved as external additions but are ignored by the shuffle engine.
    """
    state = coordinator.state
    source_tracks = state.get("source_tracks", {})
    if not source_tracks or not state.get("order"):
        return False

    now = datetime.now(timezone.utc)
    last_check = _parse_iso(state.get("target_last_check"))
    if (
        not force
        and target_meta is None
        and last_check is not None
        and now - last_check < timedelta(seconds=TARGET_RECONCILE_INTERVAL_SECONDS)
    ):
        return False

    if target_meta is None:
        target_meta = await coordinator._spotify("get_playlist", playlist_id=coordinator.target_id)

    state["target_last_check"] = now.isoformat()
    state["target_name"] = target_meta.get("name", state.get("target_name") or coordinator.target_id)
    state["target_total"] = int(
        ((target_meta.get("tracks") or {}).get("total")) or state.get("target_total") or 0
    )
    snapshot = _snapshot_id(target_meta)

    if not force and snapshot and snapshot == state.get("target_snapshot"):
        await coordinator._save()
        return False

    items, total = await _fetch_target_items(coordinator)
    played = coordinator.played_set
    old_order = [tid for tid in state.get("order", []) if tid in source_tracks]
    old_excluded = {
        tid
        for tid in state.get("manual_excluded", [])
        if tid in source_tracks and tid not in played
    }

    physical_source_ids: list[str] = []
    physical_seen: set[str] = set()
    foreign_uris: list[str] = []
    foreign_seen: set[str] = set()
    actual_first_id: str | None = None
    actual_first_name: str | None = None

    for item in items:
        track = item.get("track") or {}
        tid = track.get("id")
        uri = track.get("uri")

        if actual_first_id is None and (tid or uri):
            actual_first_id = tid
            actual_first_name = track.get("name") or tid or uri

        if tid and tid in source_tracks:
            if tid not in played and tid not in physical_seen:
                physical_seen.add(tid)
                physical_source_ids.append(tid)
        elif uri and uri not in foreign_seen:
            foreign_seen.add(uri)
            foreign_uris.append(uri)

    candidates = [tid for tid in old_order if tid not in played]
    physical_set = set(physical_source_ids)

    # Missing source tracks were manually removed from the working playlist. Keep them
    # excluded only for this cycle. If the user manually adds one back, physical_set
    # removes it from this exclusion automatically.
    manual_excluded = (old_excluded | {tid for tid in candidates if tid not in physical_set}) - physical_set

    played_order = [tid for tid in old_order if tid in played]
    excluded_tail = [tid for tid in old_order if tid in manual_excluded]
    for tid in manual_excluded:
        if tid not in excluded_tail:
            excluded_tail.append(tid)

    state["manual_excluded"] = excluded_tail
    state["manual_foreign_uris"] = foreign_uris
    state["order"] = played_order + physical_source_ids + excluded_tail
    state["target_total"] = total
    state["target_snapshot"] = snapshot
    state["target_first_track_id"] = actual_first_id
    state["target_first_track"] = actual_first_name
    state["target_order_ok"] = physical_source_ids == coordinator.remaining_ids

    await coordinator._save()
    return True


def install_manual_target_support(cls) -> None:
    """Patch TrueShuffleCoordinator with current-cycle manual target reconciliation."""
    if getattr(cls, "_manual_target_support_installed", False):
        return
    cls._manual_target_support_installed = True

    original_initialize = cls.async_initialize
    original_sync_source = cls.async_sync_source
    original_start_new_cycle = cls.async_start_new_cycle
    original_apply_changes = cls.async_apply_target_changes
    original_reshuffle = cls.async_reshuffle_remaining
    original_rebuild_remaining = cls.async_rebuild_target_remaining
    original_mark_played = cls._mark_played
    original_remaining = cls.remaining_ids.fget
    original_snapshot = cls.snapshot.fget

    async def async_initialize(self) -> None:
        await original_initialize(self)
        self.state.setdefault("manual_excluded", [])
        self.state.setdefault("manual_foreign_uris", [])
        self.state.setdefault("target_snapshot", None)
        self.state.setdefault("target_last_check", None)

    def remaining_ids(self) -> list[str]:
        excluded = set(self.state.get("manual_excluded", []))
        return [tid for tid in original_remaining(self) if tid not in excluded]

    def snapshot(self) -> dict[str, Any]:
        data = dict(original_snapshot(self))
        played = len(self.played_set)
        manually_removed = len(set(self.state.get("manual_excluded", [])))
        remaining = self.remaining_count
        cycle_total = played + manually_removed + remaining
        data["manual_excluded_count"] = manually_removed
        data["manual_foreign_count"] = len(self.state.get("manual_foreign_uris", []))
        data["remaining_count"] = remaining
        data["progress_percent"] = round(((played + manually_removed) / cycle_total) * 100, 1) if cycle_total else 0.0
        return data

    def _mark_played(self, track_id: str) -> bool:
        if track_id in self.state.get("manual_excluded", []):
            self.state["manual_excluded"] = [
                tid for tid in self.state.get("manual_excluded", []) if tid != track_id
            ]
        return original_mark_played(self, track_id)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if self._sync_due():
                await self.async_sync_source(force=False)

            await self._async_poll_playback()

            try:
                await self._async_reconcile_recent_history(force=self._target_pause_expired())
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Unable to reconcile Spotify recent history: %s", err)

            try:
                await _reconcile_target(self)
            except Exception as err:  # noqa: BLE001
                # Manual-target reconciliation is protective. A temporary Spotify API
                # failure must not take the whole coordinator down.
                _LOGGER.warning("Unable to reconcile manual TRUE SHUFFLE changes: %s", err)

            if self.state.get("pending_target_rebuild") and self._target_update_safe():
                try:
                    await _reconcile_target(self, force=True)
                except Exception:
                    pass
                await self.async_apply_target_changes()

            if (
                self.remaining_count == 0
                and self.state.get("order")
                and self.settings.get(CONF_AUTO_NEW_CYCLE, DEFAULT_AUTO_NEW_CYCLE)
                and self._target_update_safe()
            ):
                await self.async_start_new_cycle()

            return self.snapshot
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    async def async_sync_source(self, force: bool = True) -> None:
        if self.state.get("source_tracks") and self.state.get("order"):
            try:
                meta = await self._spotify("get_playlist", playlist_id=self.target_id)
                await _reconcile_target(self, force=force, target_meta=meta)
            except Exception:
                pass

        await original_sync_source(self, force=force)

        source_ids = set(self.state.get("source_tracks", {}))
        self.state["manual_excluded"] = [
            tid for tid in self.state.get("manual_excluded", []) if tid in source_ids
        ]
        await self._save()

    async def async_start_new_cycle(self) -> None:
        # A new cycle is the only point where the full source playlist becomes authority
        # again. Manual removals and external additions from the previous cycle expire.
        self.state["manual_excluded"] = []
        self.state["manual_foreign_uris"] = []
        self.state["target_snapshot"] = None
        await original_start_new_cycle(self)

    async def async_apply_target_changes(self, force: bool = False) -> None:
        await original_apply_changes(self, force=force)
        # The physical playlist changed through our own API calls. Force one future
        # reconciliation so the stored snapshot and any surviving external tracks refresh.
        self.state["target_snapshot"] = None
        await self._save()

    async def _async_full_rebuild_locked(self) -> None:
        # Same managed order as the original coordinator, plus external manual tracks.
        # New-cycle wrapper clears manual_foreign_uris first, so those tracks disappear
        # exactly when a fresh cycle is built from the source playlist.
        uris = [self.state["source_tracks"][tid]["uri"] for tid in self.remaining_ids]
        for uri in self.state.get("manual_foreign_uris", []):
            if uri and uri not in uris:
                uris.append(uri)

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

    async def async_reshuffle_remaining(self) -> None:
        if self._target_update_safe():
            try:
                await _reconcile_target(self, force=True)
            except Exception:
                pass
        await original_reshuffle(self)

    async def async_rebuild_target_remaining(self, force: bool = False) -> None:
        if not force:
            try:
                await _reconcile_target(self, force=True)
            except Exception:
                pass
        await original_rebuild_remaining(self, force=force)

    cls.async_initialize = async_initialize
    cls.remaining_ids = property(remaining_ids)
    cls.snapshot = property(snapshot)
    cls._mark_played = _mark_played
    cls._async_update_data = _async_update_data
    cls.async_sync_source = async_sync_source
    cls.async_start_new_cycle = async_start_new_cycle
    cls.async_apply_target_changes = async_apply_target_changes
    cls._async_full_rebuild_locked = _async_full_rebuild_locked
    cls.async_reshuffle_remaining = async_reshuffle_remaining
    cls.async_rebuild_target_remaining = async_rebuild_target_remaining
    cls._manual_reconcile_target = _reconcile_target
