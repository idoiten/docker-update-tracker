"""Docker Update Tracker.

One config entry per Docker host (a tecnativa/docker-socket-proxy
instance). Each entry's DataUpdateCoordinator periodically:

  1. Lists running containers via the proxy.
  2. For each, reads its current image's RepoDigests/RepoTags.
  3. Looks up the latest available digest for that image:tag from its
     registry (Docker Hub, GHCR, or anything else - see api.py).
  4. Compares current vs latest digest.

A failure looking up ONE container's registry digest (network hiccup,
image removed upstream, unusual reference we can't parse, ...) never
fails the whole coordinator update - that container is just reported
without a known latest_digest until the next successful refresh, while
every other container's data still updates normally.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DockerProxyClient, DockerProxyError, RegistryClient, RegistryError
from .const import CONF_PROXY_URL, DEFAULT_SCAN_INTERVAL_HOURS, DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


class DockerUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for one Docker host."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxy_client: DockerProxyClient,
        registry_client: RegistryClient,
        host_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({host_name})",
            update_interval=timedelta(hours=DEFAULT_SCAN_INTERVAL_HOURS),
        )
        self._proxy = proxy_client
        self._registry = registry_client
        self.host_name = host_name

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            containers = await self._proxy.list_containers()
        except DockerProxyError as err:
            raise UpdateFailed(f"Cannot reach docker-socket-proxy: {err}") from err

        results: dict[str, dict[str, Any]] = {}

        for container in containers:
            name = container.get("Names", ["?"])[0].lstrip("/")
            # Despite the Docker API's own field name "Image", this is the
            # tag reference the container was created from (e.g.
            # "alpine:latest"), not a sha256 id - the proxy's
            # /images/<x>/json endpoint accepts either interchangeably.
            image_ref = container.get("Image")
            if not image_ref:
                continue

            try:
                image_info = await self._proxy.get_image(image_ref)
            except DockerProxyError as err:
                _LOGGER.warning("Skipping %s - could not inspect image: %s", name, err)
                continue

            repo_digests: list[str] = image_info.get("RepoDigests") or []
            if not repo_digests:
                # Locally built / untagged images have no digest - nothing
                # to compare against a registry, so just skip silently.
                continue

            installed_digest = repo_digests[0].split("@", 1)[-1]
            # Deliberately NOT using image_info.get("RepoTags")[0] here - an
            # image can carry MULTIPLE tags (e.g. both "myimage:1.2.3" and
            # "myimage:latest" pointing at the same digest), and RepoTags[0]
            # isn't guaranteed to be the one THIS container tracks.
            # image_ref (the container's own "Image" field) always is.

            # Keep any previously-known latest_digest if this refresh's
            # lookup fails, so the entity doesn't flicker to "unknown".
            previous = self.data.get(name, {}) if self.data else {}
            latest_digest = previous.get("latest_digest")

            try:
                latest_digest = await self._registry.get_latest_digest(image_ref)
            except RegistryError as err:
                _LOGGER.warning(
                    "Could not check latest digest for %s (%s): %s", name, image_ref, err
                )

            labels: dict[str, str] = container.get("Labels") or {}

            results[name] = {
                "container_id": container.get("Id"),
                "image_ref": image_ref,
                "installed_digest": installed_digest,
                "latest_digest": latest_digest,
                "host_name": self.host_name,
                "display_name": labels.get("dut.friendly_name") or name,
                "icon": labels.get("dut.icon"),
            }

        return results


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Docker Update Tracker from a config entry."""
    session = async_get_clientsession(hass)
    proxy_client = DockerProxyClient(session, entry.data[CONF_PROXY_URL])
    registry_client = RegistryClient(session)

    coordinator = DockerUpdateCoordinator(
        hass, proxy_client, registry_client, entry.title
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
