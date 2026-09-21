"""Diagnostik - vad integrationen ser, utan hemligheter."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_PORT
from .coordinator import VattenKameraCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Det har hamtas av 'Ladda ner diagnostik' i Home Assistant."""
    coordinator: VattenKameraCoordinator | None = hass.data.get(entry.domain, {}).get(entry.entry_id)
    data: dict[str, Any] = {
        "adress": {
            "host": entry.data.get(CONF_HOST),
            "port": entry.data.get(CONF_PORT),
        },
        "installningar": dict(entry.options),
    }
    if coordinator is not None:
        # Svaren fran tjansten innehaller inga hemligheter (inget losenord
        # eller token), sa de kan visas som de ar.
        data["senaste"] = (coordinator.data or {}).get("latest")
        data["halsa"] = coordinator.health
    return data
