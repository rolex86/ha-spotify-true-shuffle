from __future__ import annotations

from typing import Any

from .robust_relink import RobustRelinkTrueShuffleCoordinator


class ResumeSafeTrueShuffleCoordinator(RobustRelinkTrueShuffleCoordinator):
    """Preserve resume position and rebuild the TRUE SHUFFLE queue after a pause.

    Spotify Connect can resume a cached paused track even after the physical playlist was
    cleaned in the meantime. Keeping that one track in the playlist preserves the resume
    position, but Spotify can still continue with a stale queue snapshot and stop at the
    end of the resumed track. To avoid that, the first live poll after a real resume
    re-anchors playback to the current TRUE SHUFFLE context at the same track and progress.
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
        self.state["resume_reanchor_pending"] = bool(
            stored.get("resume_reanchor_pending", False) and protected_id
        )
        self.state["resume_reanchor_last_at"] = stored.get("resume_reanchor_last_at")
        self.state["resume_reanchor_last_error"] = stored.get(
            "resume_reanchor_last_error"
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
        data["resume_reanchor_pending"] = bool(
            self.state.get("resume_reanchor_pending")
        )
        data["resume_reanchor_last_at"] = self.state.get("resume_reanchor_last_at")
        data["resume_reanchor_last_error"] = self.state.get(
            "resume_reanchor_last_error"
        )
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
            self.state["resume_reanchor_pending"] = False
            self.state["resume_reanchor_last_error"] = None
            return True

        changed = old_id != canonical_id or old_playback_id != playback_id
        self.state["resume_protected_track_id"] = canonical_id
        self.state["resume_protected_playback_track_id"] = playback_id

        # A different canonical track starts a fresh protection lifecycle. A changed raw
        # Spotify relink ID for the same canonical track does not cancel a pending reanchor.
        if old_id != canonical_id:
            self.state["resume_reanchor_pending"] = False
            self.state["resume_reanchor_last_error"] = None

        if changed or not self.state.get("resume_protected_since"):
            self.state["resume_protected_since"] = self._utcnow_iso()
        return changed

    async def _async_reanchor_resumed_track(self, canonical_id: str) -> bool:
        """Rebuild Spotify's queue around the resumed track without losing progress."""
        source_track = self.state.get("source_tracks", {}).get(canonical_id) or {}
        offset_uri = source_track.get("uri")
        progress_ms = int(self.state.get("current_progress_ms") or 0)
        if not offset_uri:
            self.state["resume_reanchor_last_error"] = "protected track has no source URI"
            return False

        try:
            await self._spotify(
                "player_media_play_context",
                context_uri=f"spotify:playlist:{self.target_id}",
                offset_uri=offset_uri,
                position_ms=max(0, progress_ms),
            )
        except Exception as err:  # noqa: BLE001 - resume must keep working even if reanchor fails
            self.state["resume_reanchor_last_error"] = str(err)
            return False

        self.state["resume_reanchor_pending"] = False
        self.state["resume_reanchor_last_at"] = self._utcnow_iso()
        self.state["resume_reanchor_last_error"] = None
        return True

    async def _async_poll_playback(self) -> None:
        """Persist the resume anchor and re-anchor Spotify once playback resumes."""
        await super()._async_poll_playback()

        target_uri = f"spotify:playlist:{self.target_id}"
        context_uri = self.state.get("current_context")
        is_playing = self.state.get("current_is_playing")
        playback_id = self.state.get("current_track_id")
        protected_id_before = self.state.get("resume_protected_track_id")
        protected_playback_id = self.state.get(
            "resume_protected_playback_track_id"
        )
        changed = False

        # Any real stop/pause/device disappearance after the protected track was already
        # counted Played means the next playback of that same track is a resume session.
        if (
            protected_id_before
            and protected_id_before in self.played_set
            and is_playing is not True
            and not self.state.get("resume_reanchor_pending")
        ):
            self.state["resume_reanchor_pending"] = True
            changed = True

        if context_uri == target_uri:
            canonical_id = self._current_canonical_track_id()

            if canonical_id:
                should_reanchor = bool(
                    is_playing is True
                    and self.state.get("resume_reanchor_pending")
                    and protected_id_before
                    and canonical_id == protected_id_before
                )

                changed = self._set_resume_protection(
                    canonical_id, playback_id
                ) or changed

                if should_reanchor:
                    if await self._async_reanchor_resumed_track(canonical_id):
                        changed = True
                    else:
                        # Keep it pending so a later poll can retry; the normal resumed
                        # playback continues untouched even if the helper call failed.
                        self.state["resume_reanchor_pending"] = True
                        changed = True

            elif (
                is_playing is True
                and playback_id
                and protected_playback_id
                and playback_id != protected_playback_id
            ):
                changed = self._set_resume_protection(None, None) or changed

        elif is_playing is True:
            # Positive playback in another context explicitly ends the TRUE SHUFFLE resume
            # lifecycle. Idle/empty states do not clear it.
            changed = self._set_resume_protection(None, None) or changed

        if changed:
            await self._save()
