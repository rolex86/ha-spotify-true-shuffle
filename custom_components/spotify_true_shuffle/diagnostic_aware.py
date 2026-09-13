from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .target_aware import TargetAwareTrueShuffleCoordinator

_LOGGER = logging.getLogger(__name__)

PLAYBACK_DIAGNOSTIC_LIMIT = 720
HISTORY_DIAGNOSTIC_LIMIT = 100
RATE_LIMIT_DIAGNOSTIC_LIMIT = 100
HISTORY_SESSION_END_SECONDS = 120
HISTORY_GENERIC_ERROR_BACKOFF_SECONDS = 900
GLOBAL_RATE_LIMIT_FALLBACK_SECONDS = 900
MIN_SAFE_POLL_SECONDS = 15


class DiagnosticAwareTrueShuffleCoordinator(TargetAwareTrueShuffleCoordinator):
    """Target-aware coordinator with diagnostics and Spotify API rate-limit protection.

    The live tracker behavior is intentionally unchanged. Playback responses are stored in
    a persistent ring buffer so a drive can be inspected afterwards. Recently Played is
    queried only after a listening session ends. Most importantly, any Spotify 429 response
    opens one global circuit breaker for every SpotifyPlus service call and the persisted
    retry-after deadline is respected across Home Assistant restarts and config-entry retries.
    """

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry)

        # Ten-second polling is unnecessary for a 30-second played threshold and creates
        # avoidable Spotify API traffic. Existing entries configured below this value are
        # clamped locally without changing the user's stored options.
        if self.update_interval and self.update_interval < timedelta(seconds=MIN_SAFE_POLL_SECONDS):
            self.update_interval = timedelta(seconds=MIN_SAFE_POLL_SECONDS)

        self.diagnostic_store = Store(
            hass,
            1,
            f"{DOMAIN}.{entry.entry_id}.diagnostics",
        )
        self._diagnostics: dict[str, list[dict[str, Any]]] = {
            "playback_polls": [],
            "history_calls": [],
            "rate_limits": [],
        }
        self._pending_playback_diagnostic: dict[str, Any] | None = None

    async def async_initialize(self) -> None:
        stored = await self.store.async_load() or {}
        await super().async_initialize()

        self.state["history_backoff_until"] = stored.get("history_backoff_until")
        self.state["history_last_error"] = stored.get("history_last_error")
        self.state["history_last_error_at"] = stored.get("history_last_error_at")
        self.state["history_last_success"] = stored.get("history_last_success")
        self.state["history_recovery_pending"] = bool(
            stored.get("history_recovery_pending", False)
        )
        self.state["target_last_active_at"] = stored.get("target_last_active_at")

        # Global Spotify API circuit breaker. These values live in the main Store so a
        # config-entry retry or HA restart cannot accidentally hammer Spotify again.
        self.state["spotify_backoff_until"] = stored.get("spotify_backoff_until")
        self.state["spotify_rate_limit_service"] = stored.get("spotify_rate_limit_service")
        self.state["spotify_rate_limit_error"] = stored.get("spotify_rate_limit_error")
        self.state["spotify_rate_limit_at"] = stored.get("spotify_rate_limit_at")
        self.state["spotify_rate_limit_retry_after"] = stored.get("spotify_rate_limit_retry_after")

        diagnostics = await self.diagnostic_store.async_load() or {}
        playback_polls = diagnostics.get("playback_polls") or []
        history_calls = diagnostics.get("history_calls") or []
        rate_limits = diagnostics.get("rate_limits") or []
        self._diagnostics = {
            "playback_polls": playback_polls[-PLAYBACK_DIAGNOSTIC_LIMIT:],
            "history_calls": history_calls[-HISTORY_DIAGNOSTIC_LIMIT:],
            "rate_limits": rate_limits[-RATE_LIMIT_DIAGNOSTIC_LIMIT:],
        }

    @property
    def snapshot(self) -> dict[str, Any]:
        data = dict(super().snapshot)
        data["diagnostic_playback_polls"] = len(
            self._diagnostics.get("playback_polls", [])
        )
        data["diagnostic_history_calls"] = len(
            self._diagnostics.get("history_calls", [])
        )
        data["diagnostic_rate_limits"] = len(
            self._diagnostics.get("rate_limits", [])
        )
        data["spotify_rate_limited"] = self._spotify_backoff_active()
        return data

    async def _save_diagnostics(self) -> None:
        try:
            await self.diagnostic_store.async_save(self._diagnostics)
        except Exception as err:  # noqa: BLE001 - diagnostics must never break playback
            _LOGGER.warning("Unable to save True Shuffle diagnostics: %s", err)

    async def _append_playback_diagnostic(self, item: dict[str, Any]) -> None:
        polls = list(self._diagnostics.get("playback_polls", []))
        polls.append(item)
        self._diagnostics["playback_polls"] = polls[-PLAYBACK_DIAGNOSTIC_LIMIT:]
        await self._save_diagnostics()

    async def _append_history_diagnostic(self, item: dict[str, Any]) -> None:
        calls = list(self._diagnostics.get("history_calls", []))
        calls.append(item)
        self._diagnostics["history_calls"] = calls[-HISTORY_DIAGNOSTIC_LIMIT:]
        await self._save_diagnostics()

    async def _append_rate_limit_diagnostic(self, item: dict[str, Any]) -> None:
        events = list(self._diagnostics.get("rate_limits", []))
        events.append(item)
        self._diagnostics["rate_limits"] = events[-RATE_LIMIT_DIAGNOSTIC_LIMIT:]
        await self._save_diagnostics()

    @staticmethod
    def _retry_after_seconds(err: Exception) -> int | None:
        match = re.search(
            r"retry-after\s*:\s*(\d+)\s*seconds?",
            str(err),
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_rate_limit_error(err: Exception) -> bool:
        text = str(err).lower()
        return "too many requests" in text or "status: 429" in text or "status 429" in text

    def _spotify_backoff_active(self) -> bool:
        until = self._parse_iso(self.state.get("spotify_backoff_until"))
        if until is None:
            return False
        return datetime.now(timezone.utc) < until

    def _spotify_backoff_remaining_seconds(self) -> int:
        until = self._parse_iso(self.state.get("spotify_backoff_until"))
        if until is None:
            return 0
        return max(0, int((until - datetime.now(timezone.utc)).total_seconds()))

    async def _set_global_rate_limit(self, service: str, err: Exception) -> None:
        now = datetime.now(timezone.utc)
        retry_after = self._retry_after_seconds(err)
        backoff_seconds = max(
            60,
            retry_after if retry_after is not None else GLOBAL_RATE_LIMIT_FALLBACK_SECONDS,
        )
        backoff_until = now + timedelta(seconds=backoff_seconds)

        self.state["spotify_backoff_until"] = backoff_until.isoformat()
        self.state["spotify_rate_limit_service"] = service
        self.state["spotify_rate_limit_error"] = str(err)
        self.state["spotify_rate_limit_at"] = now.isoformat()
        self.state["spotify_rate_limit_retry_after"] = retry_after
        self.state["status"] = "rate_limited"
        await self._save()
        await self._append_rate_limit_diagnostic(
            {
                "at": now.isoformat(),
                "service": service,
                "error": str(err),
                "retry_after_seconds": retry_after,
                "backoff_until": backoff_until.isoformat(),
            }
        )

    async def _clear_expired_global_rate_limit(self) -> None:
        if not self.state.get("spotify_backoff_until"):
            return
        if self._spotify_backoff_active():
            return

        self.state["spotify_backoff_until"] = None
        self.state["spotify_rate_limit_service"] = None
        self.state["spotify_rate_limit_error"] = None
        self.state["spotify_rate_limit_at"] = None
        self.state["spotify_rate_limit_retry_after"] = None
        if self.state.get("status") == "rate_limited":
            self.state["status"] = "running" if self.state.get("order") else "ready"
        await self._save()

    def _history_backoff_active(self) -> bool:
        until = self._parse_iso(self.state.get("history_backoff_until"))
        if until is None:
            return False
        return datetime.now(timezone.utc) < until

    def _history_recovery_due(self) -> bool:
        if not self.state.get("history_recovery_pending"):
            return False

        if self._spotify_backoff_active() or self._history_backoff_active():
            return False

        # A two-minute pause on the target playlist is our normal session-end boundary.
        if self._target_pause_expired():
            return True

        # Do not query history while the target playlist still has the active context.
        if self._target_context_active:
            return False

        last_active = self._parse_iso(self.state.get("target_last_active_at"))
        if last_active is None:
            return False

        return datetime.now(timezone.utc) - last_active >= timedelta(
            seconds=HISTORY_SESSION_END_SECONDS
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Do zero Spotify API calls while the global circuit breaker is open."""
        if self._spotify_backoff_active():
            self.state["status"] = "rate_limited"
            return self.snapshot

        await self._clear_expired_global_rate_limit()
        return await super()._async_update_data()

    async def _spotify(self, service: str, **data) -> dict[str, Any]:
        # Buttons, forced syncs and config-entry retries can reach _spotify outside the
        # normal coordinator update path. Guard every call here as well.
        if self._spotify_backoff_active():
            remaining = self._spotify_backoff_remaining_seconds()
            raise UpdateFailed(
                f"Spotify API rate-limit circuit breaker active; retry in {remaining} seconds"
            )

        await self._clear_expired_global_rate_limit()

        requested = datetime.now(timezone.utc)
        requested_ms = int(requested.timestamp() * 1000)
        try:
            result = await super()._spotify(service, **data)
        except Exception as err:
            received = datetime.now(timezone.utc)

            if service == "get_player_playback_state":
                self._pending_playback_diagnostic = {
                    "requested_at": requested.isoformat(),
                    "received_at": received.isoformat(),
                    "request_duration_ms": int(
                        (received - requested).total_seconds() * 1000
                    ),
                    "error": str(err),
                }

            if self._is_rate_limit_error(err):
                await self._set_global_rate_limit(service, err)
            raise

        # Only playback-state calls need a per-poll diagnostic record. All other Spotify
        # calls still benefit from the global 429 circuit breaker above.
        if service != "get_player_playback_state":
            return result

        received = datetime.now(timezone.utc)
        received_ms = int(received.timestamp() * 1000)
        context = result.get("context") or {}
        item = result.get("item") or {}
        device = result.get("device") or {}
        try:
            spotify_timestamp_ms = int(result.get("timestamp") or 0)
        except (TypeError, ValueError):
            spotify_timestamp_ms = 0

        self._pending_playback_diagnostic = {
            "requested_at": requested.isoformat(),
            "received_at": received.isoformat(),
            "request_duration_ms": received_ms - requested_ms,
            "spotify_timestamp_ms": spotify_timestamp_ms or None,
            "response_age_ms": (
                received_ms - spotify_timestamp_ms if spotify_timestamp_ms else None
            ),
            "is_empty": bool(result.get("is_empty")),
            "is_playing": bool(result.get("is_playing")),
            "context_uri": context.get("uri"),
            "context_type": context.get("type"),
            "track_id": item.get("id"),
            "track_name": item.get("name"),
            "progress_ms": int(result.get("progress_ms") or 0),
            "duration_ms": int(item.get("duration_ms") or 0),
            "device_name": device.get("name"),
            "device_id": device.get("id"),
            "device_type": device.get("type"),
            "shuffle_state": result.get("shuffle_state"),
            "repeat_state": result.get("repeat_state"),
            "error": None,
        }
        return result

    async def _async_poll_playback(self) -> None:
        self._pending_playback_diagnostic = None
        try:
            await super()._async_poll_playback()
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
                # Persist the session marker even if the base tracker happened to receive
                # an identical/stale response and therefore had nothing else to save.
                await self._save()

            diagnostic.update(
                {
                    "target_context_active_after": bool(self._target_context_active),
                    "tracking_track_id_after": self.state.get("tracking_track_id"),
                    "tracking_progress_ms_after": int(
                        self.state.get("tracking_progress_ms") or 0
                    ),
                    "tracking_context_lost_at_after": self.state.get(
                        "tracking_context_lost_at"
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
        """Run Recently Played only once a listening session has ended.

        The base coordinator calls this method on every update. This override turns those
        calls into cheap no-ops until a real session boundary is reached. Spotify 429
        retry-after responses are also handled by the global circuit breaker in _spotify.
        """
        if not self._history_recovery_due():
            return

        requested = datetime.now(timezone.utc)
        before_played = int(self.state.get("history_recovered_played") or 0)
        before_skipped = int(self.state.get("history_recovered_skipped") or 0)

        try:
            # We already control cadence here, so bypass the base 30-second interval gate.
            await super()._async_reconcile_recent_history(force=True)
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
            }
        )
