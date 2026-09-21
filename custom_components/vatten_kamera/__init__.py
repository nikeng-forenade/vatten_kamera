"""Vattenkamera - vardet fran pumpdisplayen in i Home Assistant.

Integrationen gors ingen bildbehandling sjalv. Den fragar vardetjansten (som
kor kameran och tolkar displayen, normalt i en LXC i Proxmox) om det senaste
vardet over HTTP och visar det som entiteter:

    sensor.vatten_kamera_niva           - vardet, t.ex. 0.58
    sensor.vatten_kamera_senast_last    - nar vardet lastes
    sensor.vatten_kamera_status         - tjänstens lage i text
    binary_sensor.vatten_kamera_lasning_ok - gick senaste lasningen bra
    button.vatten_kamera_las_nu         - starta en lasning direkt

Vardetjanstens adress anges nar integrationen laggs till (IP och port).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VattenKameraClient
from .const import CONF_ALLOW_RUN, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import VattenKameraCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Startar integrationen for en sparad adress."""
    session = async_get_clientsession(hass)
    client = VattenKameraClient(session, entry.data[CONF_HOST], entry.data[CONF_PORT])
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    coordinator = VattenKameraCoordinator(hass, client, int(scan_interval))
    # Forsta hamtningen har: ar tjansten inte uppe blir installationen
    # "retrying" i stallet for att visa fel varde.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stanger av integrationen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Laddar om nar installningarna andras."""
    await hass.config_entries.async_reload(entry.entry_id)


def allow_run(entry: ConfigEntry) -> bool:
    """Far knappen 'Las nu' anvandas?"""
    return bool(entry.options.get(CONF_ALLOW_RUN, entry.data.get(CONF_ALLOW_RUN, True)))
