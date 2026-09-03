"""Update entities for Docker Update Tracker - one per container."""
from __future__ import annotations

import logging

from homeassistant.components.update import UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DockerUpdateCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _short(digest: str | None) -> str | None:
    """Shorten a 'sha256:abcdef...' digest to a readable prefix."""
    if not digest:
        return None
    return digest.split(":", 1)[-1][:12]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up update entities for one config entry, adding new ones dynamically."""
    coordinator: DockerUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_containers: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_names = set(coordinator.data.keys()) - known_containers
        if not new_names:
            return
        known_containers.update(new_names)
        async_add_entities(
            DockerContainerUpdateEntity(coordinator, entry, name) for name in new_names
        )

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class DockerContainerUpdateEntity(CoordinatorEntity[DockerUpdateCoordinator], UpdateEntity):
    """One update entity per Docker container."""

    _attr_has_entity_name = True
    # No supported_features override needed - UpdateEntity's own default
    # (no features, no INSTALL button) is exactly what we want, since
    # this integration is informational only by design. Setting a bare
    # `0` here previously broke HA's own supported_features handling,
    # which expects a real UpdateEntityFeature flag, not a plain int.

    def __init__(
        self, coordinator: DockerUpdateCoordinator, entry: ConfigEntry, container_name: str
    ) -> None:
        super().__init__(coordinator)
        self._container_name = container_name
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{container_name}"
        self._attr_name = container_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Docker",
            model="docker-socket-proxy",
        )

    @property
    def _data(self) -> dict | None:
        return self.coordinator.data.get(self._container_name)

    @property
    def available(self) -> bool:
        return super().available and self._data is not None

    @property
    def installed_version(self) -> str | None:
        data = self._data
        return _short(data["installed_digest"]) if data else None

    @property
    def latest_version(self) -> str | None:
        data = self._data
        if not data:
            return None
        # If we don't yet know the latest digest (lookup failed / hasn't
        # run yet), report "no update known" rather than guessing - i.e.
        # same as installed, not blank.
        return _short(data["latest_digest"]) or self.installed_version

    @property
    def extra_state_attributes(self) -> dict:
        data = self._data or {}
        return {
            "image": data.get("image_ref"),
            "host": data.get("host_name"),
        }
