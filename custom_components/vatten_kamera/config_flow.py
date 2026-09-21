"""Lagger till vardetjansten: fraga efter adress och port."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VattenKameraClient, VattenKameraError
from .const import (
    CONF_ALLOW_RUN,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NAME,
)

_LOGGER = logging.getLogger(__name__)


class VattenKameraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Fragar efter adressen till vardetjansten."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            client = VattenKameraClient(async_get_clientsession(self.hass), host, port)
            try:
                health = await client.async_health()
            except VattenKameraError as exc:
                _LOGGER.debug("ingen kontakt med %s: %s", client.base_url, exc)
                errors["base"] = "ingen_kontakt"
            else:
                if not health.get("ok"):
                    errors["base"] = "fel_svar"
                else:
                    return self.async_create_entry(
                        title=f"{NAME} ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                            CONF_ALLOW_RUN: True,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="192.168.1.100"): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VattenKameraOptionsFlow()


class VattenKameraOptionsFlow(OptionsFlow):
    """Hur ofta vi fragar, och om knappen far anvandas."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_SCAN_INTERVAL,
                        self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Required(
                    CONF_ALLOW_RUN,
                    default=self.config_entry.options.get(
                        CONF_ALLOW_RUN, self.config_entry.data.get(CONF_ALLOW_RUN, True)
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
