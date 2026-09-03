"""Config flow for Docker Update Tracker."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DockerProxyClient, DockerProxyError
from .const import (
    CONF_DOCKERHUB_TOKEN,
    CONF_DOCKERHUB_USERNAME,
    CONF_GHCR_TOKEN,
    CONF_GHCR_USERNAME,
    CONF_NAME,
    CONF_PROXY_URL,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_PROXY_URL): str,
    }
)


async def _validate_connection(hass: HomeAssistant, proxy_url: str) -> None:
    """Raise on failure; used to confirm the proxy is reachable and readable."""
    session = async_get_clientsession(hass)
    client = DockerProxyClient(session, proxy_url)
    await client.list_containers()


class DockerUpdateTrackerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow - one Docker host (proxy) per entry."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "GlobalOptionsFlowHandler":
        """Global settings (scan interval, registry credentials) - editing
        these on ANY entry applies them to every Docker Update Tracker
        entry, since these aren't really per-host concerns.
        """
        return GlobalOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            proxy_url = user_input[CONF_PROXY_URL].rstrip("/")
            await self.async_set_unique_id(proxy_url)
            self._abort_if_unique_id_configured()

            try:
                await _validate_connection(self.hass, proxy_url)
            except DockerProxyError:
                errors["base"] = "cannot_connect"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_PROXY_URL: proxy_url,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )


class GlobalOptionsFlowHandler(config_entries.OptionsFlow):
    """Edits scan_interval_hours + registry credentials.

    These apply to every Docker Update Tracker host, not just the entry
    this flow happened to be opened from - Docker Hub/GHCR credentials
    and a sensible scan cadence aren't really per-host concerns. On
    submit, the same values are written to every entry's .options and
    every entry is reloaded, regardless of which one's "Configure"
    button was used to get here.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            new_options = {
                CONF_SCAN_INTERVAL_HOURS: user_input[CONF_SCAN_INTERVAL_HOURS],
                CONF_DOCKERHUB_USERNAME: user_input.get(CONF_DOCKERHUB_USERNAME, ""),
                CONF_DOCKERHUB_TOKEN: user_input.get(CONF_DOCKERHUB_TOKEN, ""),
                CONF_GHCR_USERNAME: user_input.get(CONF_GHCR_USERNAME, ""),
                CONF_GHCR_TOKEN: user_input.get(CONF_GHCR_TOKEN, ""),
            }
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                self.hass.config_entries.async_update_entry(entry, options=new_options)
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(entry.entry_id)
                )
            return self.async_create_entry(title="", data=new_options)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL_HOURS,
                    default=current.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                vol.Optional(
                    CONF_DOCKERHUB_USERNAME,
                    default=current.get(CONF_DOCKERHUB_USERNAME, ""),
                ): str,
                vol.Optional(
                    CONF_DOCKERHUB_TOKEN,
                    default=current.get(CONF_DOCKERHUB_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_GHCR_USERNAME,
                    default=current.get(CONF_GHCR_USERNAME, ""),
                ): str,
                vol.Optional(
                    CONF_GHCR_TOKEN,
                    default=current.get(CONF_GHCR_TOKEN, ""),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
