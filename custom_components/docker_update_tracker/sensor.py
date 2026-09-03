"""Sensor for Docker Update Tracker - one per host."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
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
    """Set up the per-host update count sensor."""
    coordinator: DockerUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DockerUpdateCountSensor(coordinator, entry)])


class DockerUpdateCountSensor(CoordinatorEntity[DockerUpdateCoordinator], SensorEntity):
    """Number of containers on this host with an update available."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "st"
    _attr_icon = "mdi:docker"

    def __init__(self, coordinator: DockerUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_update_count"
        self._attr_name = "Antal tillgängliga uppdateringar"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Docker",
            model="docker-socket-proxy",
        )

    @property
    def native_value(self) -> int:
        return len(self.coordinator.available_updates)

    @property
    def extra_state_attributes(self) -> dict:
        return {"containers": self.coordinator.available_updates}
