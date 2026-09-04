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

scan_interval_hours and registry credentials are GLOBAL settings, not
per-host: editing them via any single entry's "Configure" dialog writes
the same values to every Docker Update Tracker entry and reloads all of
them (see config_flow.py's GlobalOptionsFlowHandler) - a Docker Hub/GHCR
login and a sensible scan cadence aren't really per-host concerns.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DockerProxyClient,
    DockerProxyError,
    DockerProxyPermissionError,
    RegistryClient,
    RegistryError,
)
from .const import (
    CONF_DOCKERHUB_TOKEN,
    CONF_DOCKERHUB_USERNAME,
    CONF_GHCR_TOKEN,
    CONF_GHCR_USERNAME,
    CONF_PROXY_URL,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOCKER_HUB_REGISTRY,
    DOMAIN,
    GHCR_REGISTRY,
    PLATFORMS,
)

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff for the Docker events stream: start quick, cap so we
# don't hammer a genuinely-down proxy forever.
EVENT_RECONNECT_INITIAL_SECONDS = 5
EVENT_RECONNECT_MAX_SECONDS = 300


class DockerUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for one Docker host."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxy_client: DockerProxyClient,
        registry_client: RegistryClient,
        host_name: str,
        scan_interval_hours: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({host_name})",
            update_interval=timedelta(hours=scan_interval_hours),
        )
        self._proxy = proxy_client
        self._registry = registry_client
        self.host_name = host_name
        self.event_task: asyncio.Task | None = None

    @property
    def available_updates(self) -> list[str]:
        """Names of containers whose installed digest differs from latest."""
        if not self.data:
            return []
        return [
            name
            for name, info in self.data.items()
            if info.get("latest_digest")
            and info.get("installed_digest")
            and info["latest_digest"] != info["installed_digest"]
        ]

    def start_event_listener(self, hass: HomeAssistant) -> None:
        """Start the background Docker events listener, if not already running."""
        if self.event_task is not None and not self.event_task.done():
            return
        self.event_task = hass.async_create_background_task(
            self._listen_for_events(),
            name=f"{DOMAIN}_{self.host_name}_events",
        )

    def stop_event_listener(self) -> None:
        """Cancel the background listener, if running. Safe to call anytime."""
        if self.event_task is not None:
            self.event_task.cancel()
            self.event_task = None

    async def _listen_for_events(self) -> None:
        """Long-lived Docker events listener.

        On a relevant container start/die event, triggers a full refresh
        of this host (via async_request_refresh, which the coordinator
        itself debounces) rather than a narrower single-container update -
        events are relatively rare, so a full refresh per event is simple
        and correct without the extra complexity of a per-container path.

        A 403 (EVENTS not enabled on the proxy) is treated as permanent
        for this session - logs once and returns, rather than retrying
        every few seconds forever against something a restart can't fix.
        Any other failure (network blip, proxy restart, ...) backs off
        exponentially and keeps retrying indefinitely.
        """
        backoff = EVENT_RECONNECT_INITIAL_SECONDS
        while True:
            try:
                async for event in self._proxy.stream_events():
                    action = event.get("Action")
                    if action not in ("start", "die"):
                        continue
                    container_name = (
                        event.get("Actor", {}).get("Attributes", {}).get("name", "?")
                    )
                    _LOGGER.debug(
                        "Docker event (%s) for %s on %s - requesting refresh",
                        action,
                        container_name,
                        self.host_name,
                    )
                    await self.async_request_refresh()
                # The stream ended without an error, which shouldn't
                # normally happen - reconnect rather than silently giving
                # up on event-based detection for the rest of this session.
                backoff = EVENT_RECONNECT_INITIAL_SECONDS
            except DockerProxyPermissionError:
                _LOGGER.warning(
                    "Docker events not available for %s - the docker-socket-proxy "
                    "has its EVENTS section explicitly revoked (EVENTS=0). Falling "
                    "back to the regular scan interval only (no further retries "
                    "this session).",
                    self.host_name,
                )
                return
            except asyncio.CancelledError:
                raise
            except DockerProxyError as err:
                _LOGGER.debug(
                    "Docker event stream for %s disconnected (%s), reconnecting in %ds",
                    self.host_name,
                    err,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, EVENT_RECONNECT_MAX_SECONDS)

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


def _build_credentials(options: dict) -> dict[str, tuple[str, str]]:
    """Build the {registry_host: (user, token)} map from options, skipping
    any pair where either half is blank."""
    creds: dict[str, tuple[str, str]] = {}
    dh_user = options.get(CONF_DOCKERHUB_USERNAME, "")
    dh_token = options.get(CONF_DOCKERHUB_TOKEN, "")
    if dh_user and dh_token:
        creds[DOCKER_HUB_REGISTRY] = (dh_user, dh_token)

    ghcr_user = options.get(CONF_GHCR_USERNAME, "")
    ghcr_token = options.get(CONF_GHCR_TOKEN, "")
    if ghcr_user and ghcr_token:
        creds[GHCR_REGISTRY] = (ghcr_user, ghcr_token)

    return creds


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Docker Update Tracker from a config entry."""
    session = async_get_clientsession(hass)
    proxy_client = DockerProxyClient(session, entry.data[CONF_PROXY_URL])

    credentials = _build_credentials(entry.options)
    registry_client = RegistryClient(session, credentials=credentials)

    scan_interval_hours = entry.options.get(
        CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
    )

    coordinator = DockerUpdateCoordinator(
        hass, proxy_client, registry_client, entry.title, scan_interval_hours
    )
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_event_listener(hass)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(coordinator.stop_event_listener)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload this entry when its options change.

    Note: GlobalOptionsFlowHandler already triggers a reload for every
    entry itself after writing options to all of them - this listener is
    a safety net in case options ever get updated some other way.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
