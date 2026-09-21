"""Binarsensorn: gick senaste lasningen bra?"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import VattenKameraCoordinator
from .entity import VattenKameraEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: VattenKameraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LasningOkSensor(coordinator, entry)])


class LasningOkSensor(VattenKameraEntity, BinarySensorEntity):
    """Pa nar senaste lasningen gav ett varde.

    Ar den av betyder det att displayen inte gick att lasa (eller att vardet
    inte gick att tolka) - inte att tjansten ar nere. Da ar entiteten i stallet
    otillganglig.
    """

    _attr_translation_key = "lasning_ok"
    _attr_icon = "mdi:check-decagram"

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_lasning_ok"

    @property
    def is_on(self) -> bool | None:
        reading = self.coordinator.reading
        if reading is None:
            return None
        return reading.ok

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self.coordinator.reading
        extra = self.readings_extra
        if reading is not None:
            extra["visas_som"] = reading.display
        return extra
