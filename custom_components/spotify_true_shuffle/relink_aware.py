from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .const import CONF_CONTEXT_ONLY, DEFAULT_CONTEXT_ONLY
from .coordinator import CONTEXT_GRACE_SECONDS, RECENT_HISTORY_LIMIT
from .diagnostic_aware import (
    DiagnosticAwareTrueShuffleCoordinator,
    HISTORY_GENERIC_ERROR_BACKOFF_SECONDS,
)


class RelinkAwareTrueShuffleCoordinator(DiagnosticAwareTrueShuffleCoordinator):
    """Coordinator that resolves Spotify relinked IDs and robustly tracks live progress.

    Spotify may return a market-specific playback track ID that differs from the ID stored
    in the source playlist. The playback object normally exposes the original ID through
    id_origin / linked_from. This coordinator maps every playback/history item back to the
    canonical source-playlist ID before changing cycle state.
    """

    async def async_initialize(self) -> None:
        stored = await self.store.async_load() or {}
        await super().async_initialize()
        self.state["tracking_playback_track_id"] = stored.get(
            "tracking_playback_track_id"
        )
        self.state["tracking_match_method"] = stored.get("tracking_match_method")
        # History events that could not yet be classified are kept separately from the
        # API cursor. This lets the cursor advance without permanently losing opaque
        # relinks or the newest event whose listened duration is not known yet.
        retry_events = stored.get("history_retry_events") or []
        self.state["history_retry_events"] = [
            event for event in retry_events
            if isinstance(event, dict) and event.get("played_at_ms")
        ][-100:]

    def _clear_tracking(self) -> None:
        super()._clear_tracking()
        self.state["tracking_playback_track_id"] = None
        self.state["tracking_match_method"] = None

    @staticmethod
    def _track_id_from_uri(uri: str | None) -> str | None:
        if not uri or not isinstance(uri, str):
            return None
        if uri.startswith("spotify:track:"):
            return uri.rsplit(":", 1)[-1] or None
        return None

    @staticmethod
    def _norm(value: Any) -> str:
        return str(value or "").strip().casefold()

    def _track_id_candidates(self, track: dict[str, Any]) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(value: Any, method: str) -> None:
            if not value or not isinstance(value, str) or value in seen:
                return
            seen.add(value)
            candidates.append((value, method))

        add(track.get("id"), "id")
        add(track.get("id_origin"), "id_origin")
        add(self._track_id_from_uri(track.get("uri")), "uri")
        add(self._track_id_from_uri(track.get("uri_origin")), "uri_origin")

        linked = track.get("linked_from") or {}
        if isinstance(linked, dict):
            add(linked.get("id"), "linked_from.id")
            add(self._track_id_from_uri(linked.get("uri")), "linked_from.uri")

        return candidates

    def _resolve_source_track_id(
        self, track: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Return canonical source-playlist ID plus the match method used."""
        source_tracks = self.state.get("source_tracks", {})
        if not source_tracks or not track:
            return None, None

        candidates = self._track_id_candidates(track)
        for candidate, method in candidates:
            if candidate in source_tracks:
                return candidate, method

        # Future source snapshots may carry aliases. Support them without requiring a
        # full 2,700-track source rescan just to upgrade the current installation.
        candidate_ids = {candidate for candidate, _ in candidates}
        if candidate_ids:
            alias_matches: list[str] = []
            for source_id, source in source_tracks.items():
                aliases = set(source.get("aliases") or [])
                if aliases & candidate_ids:
                    alias_matches.append(source_id)
            if len(alias_matches) == 1:
                return alias_matches[0], "source_alias"

        # Defensive fallback for rare Spotify responses where relink metadata is absent.
        # Match by stable metadata only when one source item is clearly the best match.
        playback_name = self._norm(track.get("name"))
        if not playback_name:
            return None, None

        try:
            playback_duration = int(track.get("duration_ms") or 0)
        except (TypeError, ValueError):
            playback_duration = 0

        playback_artist_ids = {
            artist.get("id")
            for artist in (track.get("artists") or [])
            if isinstance(artist, dict) and artist.get("id")
        }
        playback_artist_names = {
            self._norm(artist.get("name"))
            for artist in (track.get("artists") or [])
            if isinstance(artist, dict) and artist.get("name")
        }
        playback_album_name = self._norm((track.get("album") or {}).get("name"))

        ranked: list[tuple[int, int, str]] = []
        for source_id, source in source_tracks.items():
            if self._norm(source.get("name")) != playback_name:
                continue

            source_duration = int(source.get("duration_ms") or 0)
            duration_diff = (
                abs(source_duration - playback_duration)
                if source_duration and playback_duration
                else 0
            )
            if source_duration and playback_duration and duration_diff > 3000:
                continue

            source_artist_ids = set(source.get("artist_ids") or [])
            source_artist_names = {
                self._norm(name) for name in (source.get("artist_names") or []) if name
            }
            if (
                playback_artist_ids
                and source_artist_ids
                and not (playback_artist_ids & source_artist_ids)
            ):
                continue

            score = 0
            if playback_artist_ids and playback_artist_ids == source_artist_ids:
                score += 8
            elif playback_artist_ids and source_artist_ids:
                score += 4
            if playback_artist_names and playback_artist_names == source_artist_names:
                score += 4
            elif playback_artist_names and source_artist_names & playback_artist_names:
                score += 2
            if playback_album_name and playback_album_name == self._norm(source.get("album_name")):
                score += 2
            if playback_duration and source_duration:
                if duration_diff <= 100:
                    score += 3
                elif duration_diff <= 1000:
                    score += 2
                else:
                    score += 1

            ranked.append((score, -duration_diff, source_id))

        if not ranked:
            return None, None

        ranked.sort(reverse=True)
        best_score = ranked[0][0:2]
        best = [source_id for score, neg_diff, source_id in ranked if (score, neg_diff) == best_score]
        if len(best) == 1:
            return best[0], "metadata"

        return None, "ambiguous_metadata"

    async def _async_poll_playback(self) -> None:
        """Track live playback using canonical source IDs and monotonic max progress."""
        self._pending_playback_diagnostic = None
        played_before = set(self.played_set)
        canonical_id: str | None = None
        match_method: str | None = None
        threshold_ms: int | None = None
        playback: dict[str, Any] | None = None

        try:
            playback = await self._spotify("get_player_playback_state")
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()

            if playback.get("is_empty"):
                self._target_context_active = False
                self.state["current_is_playing"] = False
                self.state["target_paused_since"] = None
                changed = False
                if self.state.get("tracking_track_id"):
                    lost_at = self._parse_iso(
                        self.state.get("tracking_context_lost_at")
                    )
                    if lost_at is None:
                        self.state["tracking_context_lost_at"] = now_iso
                        changed = True
                    elif now - lost_at >= timedelta(seconds=CONTEXT_GRACE_SECONDS):
                        changed = self._finalize_tracked_track(count_skip=False) or changed
                if changed:
                    await self._save()
                return

            context = playback.get("context") or {}
            context_uri = context.get("uri")
            target_uri = f"spotify:playlist:{self.target_id}"
            context_matches = context_uri == target_uri
            is_playing = bool(playback.get("is_playing"))
            self._target_context_active = context_matches

            item = playback.get("item") or {}
            playback_track_id = item.get("id")
            canonical_id, match_method = self._resolve_source_track_id(item)
            progress = int(playback.get("progress_ms") or 0)
            duration = int(item.get("duration_ms") or 0)

            self.state["current_track"] = item.get("name")
            artists = item.get("artists") or []
            self.state["current_artist"] = ", ".join(
                artist.get("name")
                for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            ) or None
            self.state["current_track_id"] = playback_track_id
            self.state["current_context"] = context_uri
            self.state["current_progress_ms"] = progress
            self.state["current_is_playing"] = is_playing

            changed = False

            if context_matches:
                if is_playing:
                    if self.state.get("target_paused_since") is not None:
                        self.state["target_paused_since"] = None
                        changed = True
                    self.state["recent_target_activity_ms"] = int(
                        now.timestamp() * 1000
                    )
                elif self.state.get("target_paused_since") is None:
                    self.state["target_paused_since"] = now_iso
                    changed = True
            elif self.state.get("target_paused_since") is not None:
                self.state["target_paused_since"] = None
                changed = True

            context_only = bool(
                self.settings.get(CONF_CONTEXT_ONLY, DEFAULT_CONTEXT_ONLY)
            )
            eligible_context = context_matches or not context_only

            if eligible_context and playback_track_id:
                if self.state.get("tracking_context_lost_at") is not None:
                    self.state["tracking_context_lost_at"] = None
                    changed = True

                tracked_id = self.state.get("tracking_track_id")
                tracked_playback_id = self.state.get("tracking_playback_track_id")

                # A resolved canonical ID is authoritative. If the current item cannot be
                # resolved, a changed raw playback ID is still enough to finalize the
                # previous tracked source item without falsely counting the new one.
                if tracked_id and canonical_id and tracked_id != canonical_id:
                    changed = self._finalize_tracked_track(count_skip=False) or changed
                elif (
                    tracked_id
                    and canonical_id is None
                    and tracked_playback_id
                    and playback_track_id != tracked_playback_id
                ):
                    changed = self._finalize_tracked_track(count_skip=False) or changed

                if canonical_id:
                    source_track = self.state["source_tracks"][canonical_id]
                    duration = int(duration or source_track.get("duration_ms") or 0)
                    threshold_ms = self._tracking_threshold_ms(duration)

                    if self.state.get("tracking_track_id") != canonical_id:
                        self.state["tracking_track_id"] = canonical_id
                        self.state["tracking_playback_track_id"] = playback_track_id
                        self.state["tracking_match_method"] = match_method
                        self.state["tracking_progress_ms"] = max(0, progress)
                        self.state["tracking_duration_ms"] = duration
                        self.state["tracking_last_seen"] = now_iso
                        self._last_track_seen = canonical_id
                        changed = True
                    else:
                        old_progress = int(
                            self.state.get("tracking_progress_ms") or 0
                        )
                        max_progress = max(old_progress, progress)
                        if max_progress != old_progress:
                            self.state["tracking_progress_ms"] = max_progress
                            changed = True

                        if duration and duration != int(
                            self.state.get("tracking_duration_ms") or 0
                        ):
                            self.state["tracking_duration_ms"] = duration
                            changed = True

                        if (
                            self.state.get("tracking_playback_track_id")
                            != playback_track_id
                        ):
                            self.state["tracking_playback_track_id"] = playback_track_id
                            changed = True
                        if self.state.get("tracking_match_method") != match_method:
                            self.state["tracking_match_method"] = match_method
                            changed = True

                        self.state["tracking_last_seen"] = now_iso
                        self._last_track_seen = canonical_id

                    # Always compare the freshest Spotify progress with the stored maximum.
                    # This makes the played decision independent of any stale state write.
                    effective_progress = max(
                        int(self.state.get("tracking_progress_ms") or 0),
                        progress,
                    )
                    if effective_progress != int(
                        self.state.get("tracking_progress_ms") or 0
                    ):
                        self.state["tracking_progress_ms"] = effective_progress
                        changed = True

                    if (
                        canonical_id not in self.played_set
                        and effective_progress >= threshold_ms
                    ):
                        changed = self._mark_played(canonical_id) or changed

            else:
                if self.state.get("tracking_track_id"):
                    lost_at = self._parse_iso(
                        self.state.get("tracking_context_lost_at")
                    )
                    if lost_at is None:
                        self.state["tracking_context_lost_at"] = now_iso
                        changed = True
                    elif now - lost_at >= timedelta(seconds=CONTEXT_GRACE_SECONDS):
                        changed = self._finalize_tracked_track(count_skip=False) or changed

            if changed:
                await self._save()

        finally:
            diagnostic = self._pending_playback_diagnostic
            if diagnostic is None:
                return

            target_uri = f"spotify:playlist:{self.target_id}"
            is_target_playing = (
                diagnostic.get("context_uri") == target_uri
                and bool(diagnostic.get("is_playing"))
            )
            if is_target_playing:
                self.state["history_recovery_pending"] = True
                self.state["target_last_active_at"] = diagnostic.get("received_at")
                await self._save()

            canonical_after = canonical_id
            if canonical_after is None and playback:
                canonical_after, match_method = self._resolve_source_track_id(
                    playback.get("item") or {}
                )

            if threshold_ms is None and canonical_after:
                source_track = self.state.get("source_tracks", {}).get(
                    canonical_after, {}
                )
                duration = int(
                    ((playback or {}).get("item") or {}).get("duration_ms")
                    or source_track.get("duration_ms")
                    or 0
                )
                threshold_ms = self._tracking_threshold_ms(duration)

            diagnostic.update(
                {
                    "canonical_source_track_id": canonical_after,
                    "id_match_method": match_method,
                    "threshold_ms": threshold_ms,
                    "tracking_track_id_after": self.state.get("tracking_track_id"),
                    "tracking_playback_track_id_after": self.state.get(
                        "tracking_playback_track_id"
                    ),
                    "tracking_match_method_after": self.state.get(
                        "tracking_match_method"
                    ),
                    "tracking_progress_ms_after": int(
                        self.state.get("tracking_progress_ms") or 0
                    ),
                    "tracking_context_lost_at_after": self.state.get(
                        "tracking_context_lost_at"
                    ),
                    "marked_played_this_poll": bool(
                        canonical_after
                        and canonical_after not in played_before
                        and canonical_after in self.played_set
                    ),
                    "played_count_after": len(self.played_set),
                    "pending_target_rebuild_after": bool(
                        self.state.get("pending_target_rebuild")
                    ),
                    "history_recovery_pending_after": bool(
                        self.state.get("history_recovery_pending")
                    ),
                    "spotify_backoff_until_after": self.state.get(
                        "spotify_backoff_until"
                    ),
                }
            )
            await self._append_playback_diagnostic(diagnostic)

    async def _async_reconcile_recent_history(self, force: bool = False) -> None:
        """Recover history without ever losing unresolved or trailing events.

        The Spotify history cursor is only a transport cursor. Events that cannot yet be
        resolved to a source track, plus the newest target event that has no successor
        timestamp yet, are persisted in history_retry_events and retried on the next
        session. This prevents an opaque Spotify relink from being skipped forever merely
        because the API cursor advanced past it.
        """
        if not self._history_recovery_due():
            return

        requested = datetime.now(timezone.utc)
        before_played = int(self.state.get("history_recovered_played") or 0)
        before_skipped = int(self.state.get("history_recovered_skipped") or 0)
        unresolved = 0
        retry_before = len(self.state.get("history_retry_events") or [])

        try:
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            now_ms = int(now.timestamp() * 1000)

            cursor_raw = self.state.get("history_cursor_ms")
            if cursor_raw is None:
                self.state["history_cursor_ms"] = now_ms
                self.state["history_last_check"] = now_iso
                self.state["history_retry_events"] = []
                await self._save()
            else:
                cursor = int(cursor_raw or 0)
                self.state["history_last_check"] = now_iso

                page = await self._spotify(
                    "get_player_recent_tracks",
                    limit=RECENT_HISTORY_LIMIT,
                    after=max(0, cursor),
                    limit_total=RECENT_HISTORY_LIMIT,
                )

                new_events: list[dict[str, Any]] = []
                max_fetched_ms = cursor
                for raw in page.get("items") or []:
                    if not isinstance(raw, dict):
                        continue
                    played_at_ms = self._history_item_ms(raw)
                    if played_at_ms <= cursor:
                        continue
                    max_fetched_ms = max(max_fetched_ms, played_at_ms)
                    new_events.append({**raw, "_played_at_ms": played_at_ms})

                # Merge persisted retry events with newly fetched history. Deduplicate by
                # played_at timestamp + raw track id; Spotify history timestamps are precise
                # enough for this purpose and this also avoids double-counting skips.
                combined: dict[tuple[int, str], dict[str, Any]] = {}
                for retry in self.state.get("history_retry_events") or []:
                    played_at_ms = int(retry.get("played_at_ms") or 0)
                    track = retry.get("track") or {}
                    track_id = str(track.get("id") or "")
                    if played_at_ms:
                        combined[(played_at_ms, track_id)] = {
                            "context": retry.get("context") or {},
                            "track": track,
                            "_played_at_ms": played_at_ms,
                            "_next_played_at_ms": int(
                                retry.get("next_played_at_ms") or 0
                            ),
                        }

                for event in new_events:
                    track = event.get("track") or {}
                    key = (
                        int(event.get("_played_at_ms") or 0),
                        str(track.get("id") or ""),
                    )
                    combined[key] = event

                events = sorted(
                    combined.values(),
                    key=lambda event: int(event.get("_played_at_ms") or 0),
                )

                target_uri = f"spotify:playlist:{self.target_id}"
                recent_target_ms = int(
                    self.state.get("recent_target_activity_ms") or 0
                )
                for event in new_events:
                    context = event.get("context") or {}
                    if context.get("uri") == target_uri:
                        recent_target_ms = max(
                            recent_target_ms,
                            int(event.get("_played_at_ms") or 0),
                        )
                self.state["recent_target_activity_ms"] = recent_target_ms

                recovered_played = 0
                recovered_skipped = 0
                retry_out: list[dict[str, Any]] = []

                for index, event in enumerate(events):
                    started_ms = int(event.get("_played_at_ms") or 0)
                    if not started_ms:
                        continue

                    next_started_ms = 0
                    if index + 1 < len(events):
                        next_started_ms = int(
                            events[index + 1].get("_played_at_ms") or 0
                        )
                    if not next_started_ms:
                        next_started_ms = int(
                            event.get("_next_played_at_ms") or 0
                        )

                    context = event.get("context") or {}
                    if context.get("uri") != target_uri:
                        continue

                    track = event.get("track") or {}

                    # The newest target event cannot be classified until Spotify reports a
                    # later history item. Keep it independently of the transport cursor.
                    if next_started_ms <= started_ms:
                        retry_out.append(
                            {
                                "played_at_ms": started_ms,
                                "next_played_at_ms": 0,
                                "context": context,
                                "track": track,
                            }
                        )
                        continue

                    canonical_id, _match_method = self._resolve_source_track_id(track)
                    if not canonical_id:
                        unresolved += 1
                        retry_out.append(
                            {
                                "played_at_ms": started_ms,
                                "next_played_at_ms": next_started_ms,
                                "context": context,
                                "track": track,
                            }
                        )
                        continue

                    if canonical_id in self.played_set:
                        continue

                    source_track = self.state["source_tracks"][canonical_id]
                    duration = int(
                        track.get("duration_ms")
                        or source_track.get("duration_ms")
                        or 0
                    )
                    elapsed = max(0, next_started_ms - started_ms)
                    listened_ms = min(elapsed, duration) if duration else elapsed
                    threshold = self._tracking_threshold_ms(duration)

                    if listened_ms >= threshold:
                        if self._mark_played(canonical_id):
                            recovered_played += 1
                    else:
                        self.state["skipped"] = int(
                            self.state.get("skipped", 0)
                        ) + 1
                        self._defer_skipped_track(canonical_id)
                        recovered_skipped += 1

                # The API cursor may safely move past every fetched event because anything
                # not fully classified is now persisted in retry_out.
                self.state["history_cursor_ms"] = max_fetched_ms
                self.state["history_retry_events"] = retry_out[-100:]

                if recovered_played:
                    self.state["history_recovered_played"] = int(
                        self.state.get("history_recovered_played") or 0
                    ) + recovered_played
                if recovered_skipped:
                    self.state["history_recovered_skipped"] = int(
                        self.state.get("history_recovered_skipped") or 0
                    ) + recovered_skipped
                await self._save()

        except Exception as err:
            retry_after = self._retry_after_seconds(err)
            backoff_seconds = max(
                60,
                retry_after
                if retry_after is not None
                else HISTORY_GENERIC_ERROR_BACKOFF_SECONDS,
            )
            backoff_until = datetime.now(timezone.utc) + timedelta(
                seconds=backoff_seconds
            )
            self.state["history_backoff_until"] = backoff_until.isoformat()
            self.state["history_last_error"] = str(err)
            self.state["history_last_error_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            await self._save()
            await self._append_history_diagnostic(
                {
                    "requested_at": requested.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "success": False,
                    "error": str(err),
                    "retry_after_seconds": retry_after,
                    "backoff_until": self.state.get("history_backoff_until"),
                    "global_backoff_until": self.state.get("spotify_backoff_until"),
                    "cursor_ms": self.state.get("history_cursor_ms"),
                    "unresolved_tracks": unresolved,
                    "retry_events_before": retry_before,
                    "retry_events_after": len(
                        self.state.get("history_retry_events") or []
                    ),
                }
            )
            raise

        self.state["history_recovery_pending"] = False
        self.state["history_backoff_until"] = None
        self.state["history_last_error"] = None
        self.state["history_last_error_at"] = None
        self.state["history_last_success"] = datetime.now(timezone.utc).isoformat()
        await self._save()

        await self._append_history_diagnostic(
            {
                "requested_at": requested.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "success": True,
                "error": None,
                "retry_after_seconds": None,
                "backoff_until": None,
                "global_backoff_until": None,
                "cursor_ms": self.state.get("history_cursor_ms"),
                "recovered_played": int(
                    self.state.get("history_recovered_played") or 0
                )
                - before_played,
                "recovered_skipped": int(
                    self.state.get("history_recovered_skipped") or 0
                )
                - before_skipped,
                "unresolved_tracks": unresolved,
                "retry_events_before": retry_before,
                "retry_events_after": len(
                    self.state.get("history_retry_events") or []
                ),
            }
        )
