from __future__ import annotations

import re
from typing import Any

from .rate_safe import RateSafeTrueShuffleCoordinator


class RobustRelinkTrueShuffleCoordinator(RateSafeTrueShuffleCoordinator):
    """Rate-safe coordinator with conservative fallbacks for opaque Spotify relinks.

    Spotify can return a different market/version track ID during live or offline-history
    playback without exposing a useful id_origin / linked_from mapping. The normal
    RelinkAware resolver still runs first. Only when that fails do we compare stable
    metadata and accept a match only when one source item is the unique best candidate.
    """

    # Offline/market-specific masters can differ by several seconds even when Spotify
    # presents them as the same song in the same playlist. Keep this deliberately small.
    _MAX_RELAXED_DURATION_DIFF_MS = 10_000
    _STRONG_DURATION_DIFF_MS = 3_000

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
        """Normalize common Spotify edition labels without deleting useful descriptors.

        Example: "Devils Got You Beat (Nu Disco Remix)" becomes
        "devils got you beat nu disco", matching the source title
        "Devils Got You Beat (Nu Disco)". This is only used after all exact-ID and strict
        metadata matching has failed, and still requires strong artist/duration evidence.
        """
        text = cls._normalized_title(value)
        if not text:
            return ""
        text = cls._VERSION_WORD_RE.sub(" ", text)
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
        # Keep all exact ID / id_origin / linked_from / alias and strict metadata logic.
        source_id, method = super()._resolve_source_track_id(track)
        if source_id is not None:
            return source_id, method

        source_tracks = self.state.get("source_tracks", {})
        if not source_tracks or not track:
            return None, method

        # If a future/full source refresh has stored ISRC, it is stronger than title based
        # heuristics and also survives alternate Spotify IDs/masters.
        playback_isrc = self._track_isrc(track)
        if playback_isrc:
            isrc_matches = [
                candidate_id
                for candidate_id, source in source_tracks.items()
                if str(source.get("isrc") or "").strip().upper() == playback_isrc
            ]
            if len(isrc_matches) == 1:
                return isrc_matches[0], "isrc"
            if len(isrc_matches) > 1:
                return None, "ambiguous_isrc"

        playback_title = self._normalized_title(track.get("name"))
        playback_relaxed_title = self._relaxed_title(track.get("name"))
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
            source_title = self._normalized_title(source.get("name"))
            source_relaxed_title = self._relaxed_title(source.get("name"))
            strict_title_match = source_title == playback_title
            relaxed_title_match = bool(
                playback_relaxed_title
                and source_relaxed_title
                and source_relaxed_title == playback_relaxed_title
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
            artist_overlap = bool(playback_artists & source_artists)
            artists_exact = bool(
                playback_artists
                and source_artists
                and playback_artists == source_artists
            )

            # A weak title-only match is never sufficient for opaque relinks. In
            # particular, relaxed edition names and >3 s master differences require the
            # exact same artist set. This keeps the 10-second allowance conservative.
            if playback_artists and source_artists and not artist_overlap:
                continue
            if (not strict_title_match or duration_diff > self._STRONG_DURATION_DIFF_MS) and not artists_exact:
                continue

            score = 14 if strict_title_match else 10
            if artists_exact:
                score += 8
            elif artist_overlap:
                score += 4

            if source_duration and playback_duration:
                if duration_diff <= 100:
                    score += 6
                elif duration_diff <= 1000:
                    score += 5
                elif duration_diff <= 3000:
                    score += 3
                elif duration_diff <= 6000:
                    score += 2
                else:
                    score += 1

            source_album = self._normalized_title(source.get("album_name"))
            if playback_album and source_album and playback_album == source_album:
                score += 2

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
            match_method = (
                "metadata_normalized"
                if self._normalized_title(source_tracks[best_ids[0]].get("name"))
                == playback_title
                else "metadata_relaxed_title"
            )
            return best_ids[0], match_method

        return None, "ambiguous_relaxed_metadata"

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
                    "track_isrc": self._track_isrc(item),
                    "resolved_source_track_id": source_id,
                    "resolved_source_match_method": method,
                }
            )

        return result
