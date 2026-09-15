from __future__ import annotations

import re
from typing import Any

from .rate_safe import RateSafeTrueShuffleCoordinator


class RobustRelinkTrueShuffleCoordinator(RateSafeTrueShuffleCoordinator):
    """Rate-safe coordinator with a conservative fallback for opaque Spotify relinks.

    Spotify sometimes plays a market-specific track ID that is different from the ID in
    the source playlist without exposing a useful id_origin / linked_from mapping. The
    normal RelinkAware resolver still runs first. Only when that fails do we compare a
    normalized title, duration and artist metadata and accept a match only when one source
    item is the unique best candidate.
    """

    _FEAT_GROUP_RE = re.compile(
        r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]\s*",
        flags=re.IGNORECASE,
    )
    _FEAT_SUFFIX_RE = re.compile(
        r"\s*(?:-|–|—|,)\s*(?:feat\.?|ft\.?|featuring)\s+.*$",
        flags=re.IGNORECASE,
    )
    _NON_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)

    @classmethod
    def _normalized_title(cls, value: Any) -> str:
        text = str(value or "").strip().casefold()
        if not text:
            return ""
        text = cls._FEAT_GROUP_RE.sub(" ", text)
        text = cls._FEAT_SUFFIX_RE.sub("", text)
        text = cls._NON_WORD_RE.sub(" ", text)
        return " ".join(text.split())

    @classmethod
    def _normalized_artist_names(cls, values: Any) -> set[str]:
        names: set[str] = set()
        for value in values or []:
            if isinstance(value, dict):
                value = value.get("name")
            normalized = cls._normalized_title(value)
            if normalized:
                names.add(normalized)
        return names

    def _resolve_source_track_id(
        self, track: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        # Keep all exact ID / id_origin / linked_from / alias and strict metadata logic.
        source_id, method = super()._resolve_source_track_id(track)
        if source_id is not None:
            return source_id, method

        source_tracks = self.state.get("source_tracks", {})
        if not source_tracks or not track:
            return None, method

        playback_title = self._normalized_title(track.get("name"))
        if not playback_title:
            return None, method

        try:
            playback_duration = int(track.get("duration_ms") or 0)
        except (TypeError, ValueError):
            playback_duration = 0

        playback_artists = self._normalized_artist_names(track.get("artists") or [])
        playback_album = self._normalized_title((track.get("album") or {}).get("name"))

        ranked: list[tuple[int, int, str]] = []
        for candidate_id, source in source_tracks.items():
            if self._normalized_title(source.get("name")) != playback_title:
                continue

            try:
                source_duration = int(source.get("duration_ms") or 0)
            except (TypeError, ValueError):
                source_duration = 0

            duration_diff = (
                abs(source_duration - playback_duration)
                if source_duration and playback_duration
                else 0
            )
            # Relinked regional masters can differ slightly, but a materially different
            # duration is not safe enough to auto-resolve.
            if source_duration and playback_duration and duration_diff > 5000:
                continue

            source_artists = self._normalized_artist_names(
                source.get("artist_names") or []
            )
            artist_overlap = bool(playback_artists & source_artists)

            score = 10  # normalized title match is mandatory
            if artist_overlap:
                score += 6
            if playback_artists and playback_artists == source_artists:
                score += 2

            if source_duration and playback_duration:
                if duration_diff <= 100:
                    score += 5
                elif duration_diff <= 1000:
                    score += 4
                elif duration_diff <= 3000:
                    score += 2
                else:
                    score += 1

            source_album = self._normalized_title(source.get("album_name"))
            if playback_album and source_album and playback_album == source_album:
                score += 1

            ranked.append((score, -duration_diff, candidate_id))

        if not ranked:
            return None, method

        ranked.sort(reverse=True)
        best_score = ranked[0][0:2]
        best_ids = [
            candidate_id
            for score, negative_duration_diff, candidate_id in ranked
            if (score, negative_duration_diff) == best_score
        ]
        if len(best_ids) == 1:
            return best_ids[0], "metadata_normalized"

        return None, "ambiguous_normalized_metadata"

    async def _spotify(self, service: str, **data) -> dict[str, Any]:
        result = await super()._spotify(service, **data)

        # Keep enough raw relink information in the persistent diagnostics to make future
        # mismatches immediately obvious without another manual API call during playback.
        if service == "get_player_playback_state" and self._pending_playback_diagnostic:
            item = result.get("item") or {}
            linked_from = item.get("linked_from") or {}
            source_id, method = self._resolve_source_track_id(item)
            self._pending_playback_diagnostic.update(
                {
                    "track_id_origin": item.get("id_origin"),
                    "track_uri_origin": item.get("uri_origin"),
                    "linked_from_id": (
                        linked_from.get("id") if isinstance(linked_from, dict) else None
                    ),
                    "resolved_source_track_id": source_id,
                    "resolved_source_match_method": method,
                }
            )

        return result
