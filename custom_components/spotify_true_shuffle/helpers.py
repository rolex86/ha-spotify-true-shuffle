from __future__ import annotations

import re

_PLAYLIST_URL_RE = re.compile(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")


def extract_playlist_id(value: str) -> str:
    value = value.strip()
    if value.startswith("spotify:playlist:"):
        playlist_id = value.rsplit(":", 1)[-1]
        if _PLAYLIST_ID_RE.fullmatch(playlist_id):
            return playlist_id
    match = _PLAYLIST_URL_RE.search(value)
    if match:
        return match.group(1)
    if _PLAYLIST_ID_RE.fullmatch(value):
        return value
    raise ValueError("Invalid Spotify playlist ID, URI or URL")
