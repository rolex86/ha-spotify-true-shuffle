from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALBUM_GAP, CONF_ARTIST_GAP, CONF_AUTO_NEW_CYCLE, CONF_CONTEXT_ONLY,
    CONF_MIN_PERCENT, CONF_MIN_SECONDS, CONF_POLL_INTERVAL, CONF_SOURCE_PLAYLIST,
    CONF_SPOTIFYPLUS_ENTITY, CONF_SYNC_INTERVAL, CONF_TARGET_PLAYLIST,
    DEFAULT_ALBUM_GAP, DEFAULT_ARTIST_GAP, DEFAULT_AUTO_NEW_CYCLE,
    DEFAULT_CONTEXT_ONLY, DEFAULT_MIN_PERCENT, DEFAULT_MIN_SECONDS,
    DEFAULT_POLL_INTERVAL, DEFAULT_SYNC_INTERVAL, DOMAIN, SPOTIFYPLUS_DOMAIN,
)
from .helpers import extract_playlist_id


def _schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_SPOTIFYPLUS_ENTITY, default=d.get(CONF_SPOTIFYPLUS_ENTITY)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player", integration="spotifyplus")
        ),
        vol.Required(CONF_SOURCE_PLAYLIST, default=d.get(CONF_SOURCE_PLAYLIST, "")): str,
        vol.Required(CONF_TARGET_PLAYLIST, default=d.get(CONF_TARGET_PLAYLIST, "")): str,
        vol.Required(CONF_ARTIST_GAP, default=d.get(CONF_ARTIST_GAP, DEFAULT_ARTIST_GAP)): vol.All(vol.Coerce(int), vol.Range(min=0, max=50)),
        vol.Required(CONF_ALBUM_GAP, default=d.get(CONF_ALBUM_GAP, DEFAULT_ALBUM_GAP)): vol.All(vol.Coerce(int), vol.Range(min=0, max=20)),
        vol.Required(CONF_MIN_SECONDS, default=d.get(CONF_MIN_SECONDS, DEFAULT_MIN_SECONDS)): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
        vol.Required(CONF_MIN_PERCENT, default=d.get(CONF_MIN_PERCENT, DEFAULT_MIN_PERCENT)): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Required(CONF_SYNC_INTERVAL, default=d.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL)): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        vol.Required(CONF_POLL_INTERVAL, default=d.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)): vol.All(vol.Coerce(int), vol.Range(min=5, max=60)),
        vol.Required(CONF_CONTEXT_ONLY, default=d.get(CONF_CONTEXT_ONLY, DEFAULT_CONTEXT_ONLY)): bool,
        vol.Required(CONF_AUTO_NEW_CYCLE, default=d.get(CONF_AUTO_NEW_CYCLE, DEFAULT_AUTO_NEW_CYCLE)): bool,
    })


async def _spotify_result(hass, service: str, entity_id: str, **data):
    if not hass.services.has_service(SPOTIFYPLUS_DOMAIN, service):
        raise RuntimeError("spotifyplus_missing")
    response = await hass.services.async_call(
        SPOTIFYPLUS_DOMAIN, service, {"entity_id": entity_id, **data},
        blocking=True, return_response=True,
    )
    if not isinstance(response, dict) or "result" not in response:
        raise RuntimeError("spotifyplus_invalid_response")
    return response["result"]


class SpotifyTrueShuffleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                source_id = extract_playlist_id(user_input[CONF_SOURCE_PLAYLIST])
                target_id = extract_playlist_id(user_input[CONF_TARGET_PLAYLIST])
                if source_id == target_id:
                    errors["base"] = "same_playlist"
                else:
                    entity_id = user_input[CONF_SPOTIFYPLUS_ENTITY]
                    source = await _spotify_result(self.hass, "get_playlist", entity_id, playlist_id=source_id)
                    target = await _spotify_result(self.hass, "get_playlist", entity_id, playlist_id=target_id)
                    data = dict(user_input)
                    data[CONF_SOURCE_PLAYLIST] = source_id
                    data[CONF_TARGET_PLAYLIST] = target_id
                    await self.async_set_unique_id(f"{entity_id}:{target_id}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"{source.get('name', source_id)} → {target.get('name', target_id)}",
                        data=data,
                    )
            except ValueError:
                errors["base"] = "invalid_playlist"
            except Exception:
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", data_schema=_schema(user_input), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SpotifyTrueShuffleOptionsFlow(config_entry)


class SpotifyTrueShuffleOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            try:
                source_id = extract_playlist_id(user_input[CONF_SOURCE_PLAYLIST])
                target_id = extract_playlist_id(user_input[CONF_TARGET_PLAYLIST])
                if source_id == target_id:
                    errors["base"] = "same_playlist"
                else:
                    user_input = dict(user_input)
                    user_input[CONF_SOURCE_PLAYLIST] = source_id
                    user_input[CONF_TARGET_PLAYLIST] = target_id
                    return self.async_create_entry(title="", data=user_input)
            except ValueError:
                errors["base"] = "invalid_playlist"
        return self.async_show_form(step_id="init", data_schema=_schema(current), errors=errors)
