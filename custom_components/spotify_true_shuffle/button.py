from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

BUTTONS = (
    ButtonEntityDescription(key="sync", name="Sync source", icon="mdi:cloud-sync"),
    ButtonEntityDescription(key="new_cycle", name="Start new cycle", icon="mdi:shuffle-variant"),
    ButtonEntityDescription(key="reshuffle", name="Reshuffle remaining", icon="mdi:shuffle"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(TrueShuffleButton(coordinator, entry, description) for description in BUTTONS)


class TrueShuffleButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, description: ButtonEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._entry = entry

    async def async_press(self) -> None:
        if self.entity_description.key == "sync":
            await self.coordinator.async_sync_source(force=True)
        elif self.entity_description.key == "new_cycle":
            await self.coordinator.async_start_new_cycle()
        elif self.entity_description.key == "reshuffle":
            await self.coordinator.async_reshuffle_remaining()
        await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Spotify True Shuffle",
            manufacturer="rolex86",
            model="True Shuffle Engine",
        )
