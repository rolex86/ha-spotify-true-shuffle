from __future__ import annotations

from typing import Any

from .robust_relink import RobustRelinkTrueShuffleCoordinator


class ResumeSafeTrueShuffleCoordinator(RobustRelinkTrueShuffleCoordinator):
    """Keep the last TRUE SHUFFLE track physically present until playback moves on.

    Spotify Connect can remember a paused track across a car disconnect even after the
    active device/context disappears from the Web API. If that already-played item is
    removed from the target playlist during the normal two-minute cleanup, Spotify can
    resume the cached item later but has no reliable next playlist item to continue with.

    This layer therefore persists one resume candidate. It is allowed to remain physically
    in TRUE SHUFFLE even after it is logically Played. Protection is released only after
    Spotify positively reports another playing track (or playback starts in another
    context), so temporary device/context loss cannot delete the resume anchor.
    """

    async def async_initialize(self) -> None:
        stored = await self.store.async_load() or {}
        await super().async_initialize()

        protected_id = stored.get("resume_protected_track_id")
        if protected_id and protected_id not in self.state.get("source_tracks", {}):
            protected_id = None

        self.state["resume_protected_track_id"] = protected_id
        self.state["resume_protected_playback_track_id"] = (
            stored.get("resume_protected_playback_track_id") if protected_id else None
        )
        self.state["resume_protected_since"] = (
            stored.get("resume_protected_since") if protected_id else None
        )

    @property
    def snapshot(self) -> dict[str, Any]:
        data = dict(super().snapshot)
        protected_id = self._paused_protected_track_id()
        data["resume_track_protected"] = protected_id is not None
        data["resume_protected_track_id"] = self.state.get(
            "resume_protected_track_id"
        )
        data["resume_protected_playback_track_id"] = self.state.get(
            "resume_protected_playback_track_id"
        )
        data["resume_protected_since"] = self.state.get("resume_protected_since")
        return data

    def _current_canonical_track_id(self) -> str | None:
        """Resolve the currently reported playback item without another Spotify call."""
        playback_id = self.state.get("current_track_id")
        tracked_id = self.state.get("tracking_track_id")
        tracked_playback_id = self.state.get("tracking_playback_track_id")

        if tracked_id and (
            not tracked_playback_id
            or not playback_id
            or tracked_playback_id == playback_id
        ):
            return tracked_id

        protected_id = self.state.get("resume_protected_track_id")
        protected_playback_id = self.state.get(
            "resume_protected_playback_track_id"
        )
        if (
            protected_id
            and playback_id
            and protected_playback_id == playback_id
        ):
            return protected_id

        if playback_id:
            canonical_id, _method = self._resolve_source_track_id({"id": playback_id})
            return canonical_id

        return None

    def _paused_protected_track_id(self) -> str | None:
        """Return the persisted played track that cleanup must not remove yet."""
        protected_id = self.state.get("resume_protected_track_id")
        if (
            protected_id
            and protected_id in self.state.get("source_tracks", {})
            and protected_id in self.played_set
        ):
            return protected_id

        # Migration fallback for the first poll after upgrading from the old transient
        # pause protection. Once _async_poll_playback runs, the result becomes persistent.
        return super()._paused_protected_track_id()

    def _set_resume_protection(
        self, canonical_id: str | None, playback_id: str | None
    ) -> bool:
        old_id = self.state.get("resume_protected_track_id")
        old_playback_id = self.state.get("resume_protected_playback_track_id")

        if canonical_id is None:
            if old_id is None and old_playback_id is None:
                return False
            self.state["resume_protected_track_id"] = None
            self.state["resume_protected_playback_track_id"] = None
            self.state["resume_protected_since"] = None
            return True

        changed = old_id != canonical_id or old_playback_id != playback_id
        self.state["resume_protected_track_id"] = canonical_id
        self.state["resume_protected_playback_track_id"] = playback_id
        if changed or not self.state.get("resume_protected_since"):
            self.state["resume_protected_since"] = self._utcnow_iso()
        return changed

    async def _async_poll_playback(self) -> None:
        """Persist the resume anchor after the normal live tracker has resolved the item."""
        await super()._async_poll_playback()

        target_uri = f"spotify:playlist:{self.target_id}"
        context_uri = self.state.get("current_context")
        is_playing = self.state.get("current_is_playing")
        playback_id = self.state.get("current_track_id")
        protected_playback_id = self.state.get(
            "resume_protected_playback_track_id"
        )
        changed = False

        if context_uri == target_uri:
            canonical_id = self._current_canonical_track_id()

            if canonical_id:
                # Keep the current TRUE SHUFFLE item as the resume anchor while it is
                # playing as well as while paused. That closes the tiny race where the car
                # disappears before Spotify ever exposes a stable paused state to HA.
                changed = self._set_resume_protection(
                    canonical_id, playback_id
                ) or changed
            elif (
                is_playing is True
                and playback_id
                and protected_playback_id
                and playback_id != protected_playback_id
            ):
                # Spotify definitely moved to another target item, but this unusual item
                # could not be resolved. Releasing the old anchor is still safe; keeping it
                # would otherwise leave an already-finished song stuck in the playlist.
                changed = self._set_resume_protection(None, None) or changed

        elif is_playing is True:
            # Positive playback in another context means the old TRUE SHUFFLE resume state
            # is no longer what Spotify will resume next. Temporary idle/empty states do
            # NOT clear the anchor.
            changed = self._set_resume_protection(None, None) or changed

        if changed:
            await self._save()
