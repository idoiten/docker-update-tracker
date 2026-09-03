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
from .const import CONF_NAME, CONF_PROXY_URL, DOMAIN

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
