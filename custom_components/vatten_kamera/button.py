"""Knappen 'Las nu' - startar en lasning direkt fran Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import allow_run
from .api import VattenKameraError
from .const import DOMAIN
from .coordinator import VattenKameraCoordinator
from .entity import VattenKameraEntity

_LOGGER = logging.getLogger(__name__)

# En lasning tar en dryg minut. Vi fragar tjansten om vardet ar klart sa lange.
MAX_VANTAN_S = 240
FRAGA_VAR_S = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: VattenKameraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LasNuButton(coordinator, entry)])


class LasNuButton(VattenKameraEntity, ButtonEntity):
    """Ber vardetjansten lasa displayen nu."""

    _attr_translation_key = "las_nu"
    _attr_icon = "mdi:camera-retake"

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_las_nu"

    @property
    def available(self) -> bool:
        """Knappen ar bara anvandbar om tjansten far lasta och svarar."""
        if not super().available:
            return False
        if not allow_run(self.entry):
            return False
        # Tjansten sjalv kan ha stangt av lasning direkt (STATUS_ALLOW_RUN).
        return bool(self.coordinator.health.get("far_lasa", True))

    async def async_press(self) -> None:
        """Startar en lasning och vantar in vardet."""
        try:
            svar = await self.coordinator.client.async_start_run()
        except VattenKameraError as exc:
            raise HomeAssistantError(f"kunde inte starta lasningen: {exc}") from exc

        if not svar.get("ok", True):
            raise HomeAssistantError(str(svar.get("text") or "lasningen kunde inte startas"))

        _LOGGER.info("lasning startad: %s", svar.get("text") or "ok")
        self.hass.async_create_task(self._async_vanta_pa_vardet())

    async def _async_vanta_pa_vardet(self) -> None:
        """Fragar tjansten med jamna mellanrum tills lasningen ar klar."""
        vantat = 0
        while vantat < MAX_VANTAN_S:
            await asyncio.sleep(FRAGA_VAR_S)
            vantat += FRAGA_VAR_S
            await self.coordinator.async_refresh()
            if not self.coordinator.health.get("kor"):
                break

        # En sista hamtning sa att vardet syns direkt i granssnittet.
        await self.coordinator.async_refresh()
        reading = self.coordinator.reading
        _LOGGER.info("lasningen klar: %s", reading.display if reading else "inget varde")
