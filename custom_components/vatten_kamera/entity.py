"""Gemensam bas for entiteterna."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import VattenKameraCoordinator


class VattenKameraEntity(CoordinatorEntity[VattenKameraCoordinator]):
    """En entitet som hor till en sparad vardetjanst."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        reading = coordinator.reading
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="nikeng-forenade",
            model="Pumpdisplay last med kamera",
            configuration_url=coordinator.client.base_url,
            sw_version=reading.version if reading and reading.version else None,
        )

    @property
    def readings_extra(self) -> dict:
        """Det som ar gemensamt nyttigt att visa pa alla entiteter."""
        reading = self.coordinator.reading
        if reading is None:
            return {}
        extra: dict = {
            "konfidens": round(reading.confidence, 3),
            "roster": reading.votes,
            "bilder": reading.frames,
        }
        if reading.read_at:
            extra["last"] = reading.read_at.isoformat(timespec="seconds")
        if reading.image_url:
            extra["bild"] = reading.image_url
        if reading.error:
            extra["fel"] = reading.error
        health = self.coordinator.health
        if health:
            extra["lage"] = health.get("lage")
            extra["nasta_korning"] = health.get("nasta_korning")
            extra["laser_nu"] = health.get("kor")
        return extra
