"""Binary sensor for Docker Update Tracker - one per host."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DockerUpdateCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the per-host 'update available' binary sensor."""
    coordinator: DockerUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DockerUpdateAvailableBinarySensor(coordinator, entry)])


class DockerUpdateAvailableBinarySensor(
    CoordinatorEntity[DockerUpdateCoordinator], BinarySensorEntity
):
    """On if any container on this host has an update available."""

    _attr_has_entity_name = False
    _attr_device_class = BinarySensorDeviceClass.UPDATE

    def __init__(self, coordinator: DockerUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_update_available"
        self._attr_name = "Uppdatering tillgänglig"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Docker",
            model="docker-socket-proxy",
        )

    @property
    def is_on(self) -> bool:
        return len(self.coordinator.available_updates) > 0

    @property
    def extra_state_attributes(self) -> dict:
        return {"containers": self.coordinator.available_updates}
