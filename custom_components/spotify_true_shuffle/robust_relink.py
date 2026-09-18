from __future__ import annotations

import re
from typing import Any

from .rate_safe import RateSafeTrueShuffleCoordinator


class RobustRelinkTrueShuffleCoordinator(RateSafeTrueShuffleCoordinator):
    """Resolve opaque Spotify relinks and alternate catalogue editions conservatively."""

    _MAX_RELAXED_DURATION_DIFF_MS = 10_000

    _FEAT_GROUP_RE = re.compile(
        r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]\s*",
        flags=re.IGNORECASE,
    )
    _FEAT_SUFFIX_RE = re.compile(
        r"\s*(?:-|–|—|,)\s*(?:feat\.?|ft\.?|featuring)\s+.*$",
        flags=re.IGNORECASE,
    )
    _VERSION_WORD_RE = re.compile(
        r"\b(?:remix|mix|edit|version|remaster|remastered)\b",
        flags=re.IGNORECASE,
    )
    _EDITION_MARKER_RE = re.compile(
        r"\b(?:radio|original|extended|club|remix|mix|edit|version|remaster|remastered)\b",
        flags=re.IGNORECASE,
    )
    _TRAILING_EDITION_RE = re.compile(r"\s+(?:-|–|—)\s+(.+)$", flags=re.IGNORECASE)
    _BRACKET_GROUP_RE = re.compile(r"\s*[\(\[]([^\)\]]+)[\)\]]\s*$", flags=re.IGNORECASE)
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
    def _relaxed_title(cls, value: Any) -> str:
        """Remove version words while preserving useful descriptors such as 'nu disco'."""
        text = cls._normalized_title(value)
        if not text:
            return ""
        text = cls._VERSION_WORD_RE.sub(" ", text)
        return " ".join(text.split())

    @classmethod
    def _edition_base_title(cls, value: Any) -> str:
        """Strip a trailing edition label such as 'Radio Edit' or 'Original Mix'.

        This intentionally runs only in the final metadata fallback. The resolver still
        requires matching artists, a close duration and a unique best source candidate.
        """
        raw = str(value or "").strip().casefold()
        if not raw:
            return ""

        raw = cls._FEAT_GROUP_RE.sub(" ", raw)
        raw = cls._FEAT_SUFFIX_RE.sub("", raw)

        bracket = cls._BRACKET_GROUP_RE.search(raw)
        if bracket and cls._EDITION_MARKER_RE.search(bracket.group(1)):
            raw = raw[: bracket.start()]

        trailing = cls._TRAILING_EDITION_RE.search(raw)
        if trailing and cls._EDITION_MARKER_RE.search(trailing.group(1)):
            raw = raw[: trailing.start()]

        raw = cls._NON_WORD_RE.sub(" ", raw)
        return " ".join(raw.split())

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

    @staticmethod
    def _track_isrc(track: dict[str, Any]) -> str | None:
        external_ids = track.get("external_ids") or {}
        if not isinstance(external_ids, dict):
            return None
        isrc = external_ids.get("isrc")
        if not isrc:
            return None
        return str(isrc).strip().upper() or None

    def _resolve_source_track_id(
        self, track: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        # Exact ID / id_origin / linked_from / aliases and the strict legacy metadata
        # resolver always get first chance.
        source_id, method = super()._resolve_source_track_id(track)
        if source_id is not None:
            return source_id, method

        source_tracks = self.state.get("source_tracks", {})
        if not source_tracks or not track:
            return None, method

        playback_isrc = self._track_isrc(track)
        if playback_isrc:
            isrc_matches = [
                candidate_id
                for candidate_id, source in source_tracks.items()
                if str(source.get("isrc") or "").strip().upper() == playback_isrc
            ]
            if len(isrc_matches) == 1:
                return isrc_matches[0], "isrc"
            # Do not stop on duplicate ISRCs; title/artist/duration can still disambiguate.

        playback_title = self._normalized_title(track.get("name"))
        playback_relaxed = self._relaxed_title(track.get("name"))
        playback_base = self._edition_base_title(track.get("name"))
        if not playback_title:
            return None, method

        try:
            playback_duration = int(track.get("duration_ms") or 0)
        except (TypeError, ValueError):
            playback_duration = 0

        playback_artists = self._normalized_artist_names(track.get("artists") or [])
        playback_album = self._normalized_title((track.get("album") or {}).get("name"))

        ranked: list[tuple[int, int, int, str]] = []

        for candidate_id, source in source_tracks.items():
            source_title = self._normalized_title(source.get("name"))
            source_relaxed = self._relaxed_title(source.get("name"))
            source_base = self._edition_base_title(source.get("name"))

            strict_title_match = source_title == playback_title
            relaxed_title_match = bool(
                (playback_relaxed and source_relaxed == playback_relaxed)
                or (playback_base and source_base == playback_base)
            )
            if not strict_title_match and not relaxed_title_match:
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
            if (
                source_duration
                and playback_duration
                and duration_diff > self._MAX_RELAXED_DURATION_DIFF_MS
            ):
                continue

            source_artists = self._normalized_artist_names(
                source.get("artist_names") or []
            )
            overlap = playback_artists & source_artists
            if playback_artists and source_artists and not overlap:
                continue

            # Edition-title matching must be backed by real artist evidence. This prevents
            # generic song names with a similar duration from being auto-resolved.
            if not strict_title_match and not overlap:
                continue

            source_isrc = str(source.get("isrc") or "").strip().upper() or None
            score = 20 if strict_title_match else 12

            if playback_isrc and source_isrc and playback_isrc == source_isrc:
                score += 50

            if playback_artists and source_artists:
                if playback_artists == source_artists:
                    score += 14
                else:
                    overlap_count = len(overlap)
                    smaller = max(1, min(len(playback_artists), len(source_artists)))
                    score += overlap_count * 4
                    score += int(6 * (overlap_count / smaller))

            if source_duration and playback_duration:
                if duration_diff <= 100:
                    score += 10
                elif duration_diff <= 1000:
                    score += 8
                elif duration_diff <= 3000:
                    score += 6
                elif duration_diff <= 6000:
                    score += 3
                else:
                    score += 1

            source_album = self._normalized_title(source.get("album_name"))
            if playback_album and source_album and playback_album == source_album:
                score += 2

            # For a relaxed edition match require a substantial combined signal.
            if not strict_title_match and score < 28:
                continue

            ranked.append((score, len(overlap), -duration_diff, candidate_id))

        if not ranked:
            return None, method

        ranked.sort(reverse=True)
        best_signature = ranked[0][0:3]
        best_ids = [
            candidate_id
            for score, overlap_count, negative_duration_diff, candidate_id in ranked
            if (score, overlap_count, negative_duration_diff) == best_signature
        ]
        if len(best_ids) != 1:
            return None, "ambiguous_relaxed_metadata"

        best_id = best_ids[0]
        best_source = source_tracks[best_id]
        match_method = (
            "metadata_normalized"
            if self._normalized_title(best_source.get("name")) == playback_title
            else "metadata_edition"
        )
        return best_id, match_method

    async def _spotify(self, service: str, **data) -> dict[str, Any]:
        result = await super()._spotify(service, **data)

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
                    "track_isrc": self._track_isrc(item),
                    "resolved_source_track_id": source_id,
                    "resolved_source_match_method": method,
                }
            )

        return result
