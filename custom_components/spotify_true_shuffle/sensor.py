from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class TrueShuffleSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


SENSORS = (
    TrueShuffleSensorDescription(key="status", name="Status", icon="mdi:shuffle-variant", value_fn=lambda d: d.get("status")),
    TrueShuffleSensorDescription(key="source_tracks", name="Source tracks", icon="mdi:playlist-music", value_fn=lambda d: d.get("source_total", 0)),
    TrueShuffleSensorDescription(key="source_unique", name="Unique source tracks", icon="mdi:playlist-check", value_fn=lambda d: d.get("source_unique", 0)),
    TrueShuffleSensorDescription(key="duplicates", name="Source duplicates", icon="mdi:content-duplicate", value_fn=lambda d: d.get("source_duplicates", 0)),
    TrueShuffleSensorDescription(key="cycle", name="Cycle", icon="mdi:sync", value_fn=lambda d: d.get("cycle", 0)),
    TrueShuffleSensorDescription(key="played", name="Played", icon="mdi:check-circle-outline", value_fn=lambda d: d.get("played_count", 0)),
    TrueShuffleSensorDescription(key="remaining", name="Remaining", icon="mdi:playlist-play", value_fn=lambda d: d.get("remaining_count", 0)),
    TrueShuffleSensorDescription(key="progress", name="Cycle progress", icon="mdi:progress-check", native_unit_of_measurement=PERCENTAGE, value_fn=lambda d: d.get("progress_percent", 0)),
    TrueShuffleSensorDescription(key="skipped", name="Skipped", icon="mdi:skip-next", value_fn=lambda d: d.get("skipped", 0)),
    TrueShuffleSensorDescription(key="added", name="Added this cycle", icon="mdi:playlist-plus", value_fn=lambda d: d.get("added_this_cycle", 0)),
    TrueShuffleSensorDescription(key="current_track", name="Current track", icon="mdi:music", value_fn=lambda d: d.get("current_track")),
    TrueShuffleSensorDescription(key="current_artist", name="Current artist", icon="mdi:account-music", value_fn=lambda d: d.get("current_artist")),
    TrueShuffleSensorDescription(key="source_name", name="Source playlist", icon="mdi:playlist-music-outline", value_fn=lambda d: d.get("source_name")),
    TrueShuffleSensorDescription(key="target_name", name="True Shuffle playlist", icon="mdi:shuffle", value_fn=lambda d: d.get("target_name")),
    TrueShuffleSensorDescription(key="last_sync", name="Last source sync", icon="mdi:cloud-sync-outline", value_fn=lambda d: d.get("last_sync")),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(TrueShuffleSensor(coordinator, entry, description) for description in SENSORS)


class TrueShuffleSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, description: TrueShuffleSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._entry = entry

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Spotify True Shuffle",
            manufacturer="rolex86",
            model="True Shuffle Engine",
        )
