"""Sensorerna: nivan, nar den lastes och tjanstens lage."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import VattenKameraCoordinator
from .entity import VattenKameraEntity

# Lagena som statussensorn kan visa.
STATUS_LAGEN = ("ok", "laser_nu", "ingen_lasning", "okontaktbar")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Skapar de tre sensorerna."""
    coordinator: VattenKameraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            NivaSensor(coordinator, entry),
            SenastLastSensor(coordinator, entry),
            StatusSensor(coordinator, entry),
        ]
    )


class NivaSensor(VattenKameraEntity, SensorEntity):
    """Vardet fran pumpdisplayen - liter kvar innan spolning."""

    _attr_translation_key = "niva"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_niva"

    @property
    def native_value(self) -> float | None:
        reading = self.coordinator.reading
        if reading is None or not reading.ok:
            # Ingen giltig lasning: visa inget varde i stallet for ett gammalt.
            return None
        return reading.numeric

    @property
    def native_unit_of_measurement(self) -> str | None:
        reading = self.coordinator.reading
        if reading is None or not reading.unit:
            return None
        return reading.unit

    @property
    def suggested_display_precision(self) -> int | None:
        reading = self.coordinator.reading
        if reading is None:
            return None
        return reading.decimals

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self.coordinator.reading
        if reading is None:
            return {}
        extra: dict[str, Any] = {
            "siffror": reading.value,
            "visas_som": reading.display,
        }
        extra.update(self.readings_extra)
        return extra


class SenastLastSensor(VattenKameraEntity, SensorEntity):
    """Nar vardet lastes - sa att ett gammalt varde syns direkt."""

    _attr_translation_key = "senast_last"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_senast_last"

    @property
    def native_value(self):
        reading = self.coordinator.reading
        if reading is None:
            return None
        return reading.read_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.readings_extra


class StatusSensor(VattenKameraEntity, SensorEntity):
    """Tjanstens lage: laser, vantar, ingen lasning eller okontaktbar."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATUS_LAGEN)
    _attr_icon = "mdi:water-pump"

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        reading = self.coordinator.reading
        if reading is None:
            return "okontaktbar"
        if self.coordinator.health.get("kor"):
            return "laser_nu"
        if reading.ok:
            return "ok"
        return "ingen_lasning"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        extra = self.readings_extra
        extra["version"] = self.coordinator.reading.version if self.coordinator.reading else None
        return extra
