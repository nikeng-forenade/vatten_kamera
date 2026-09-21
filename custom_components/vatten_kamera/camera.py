"""Kameran: bilden som värdet lästes ur.

Värdet kommer från en bild av pumpdisplayen, och den bilden är det enda sättet
att själv se att avläsningen stämmer. Här blir den en vanlig kamera-entitet i
Home Assistant — klicka på den så ser du displayen som läsningen byggde på.
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import VattenKameraError
from .const import DOMAIN
from .coordinator import VattenKameraCoordinator
from .entity import VattenKameraEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: VattenKameraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SenasteBildCamera(coordinator, entry)])


class SenasteBildCamera(VattenKameraEntity, Camera):
    """Senaste bevisbilden — utsnittet av displayen som värdet lästes ur."""

    _attr_translation_key = "senaste_bild"
    _attr_icon = "mdi:image-frame"

    def __init__(self, coordinator: VattenKameraCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_senaste_bild"

    @property
    def available(self) -> bool:
        reading = self.coordinator.reading
        return super().available and reading is not None and bool(reading.image_url)

    @property
    def is_on(self) -> bool:
        return self.available

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Hämtar bilden från tjänsten."""
        reading = self.coordinator.reading
        if reading is None or not reading.image_url:
            return None
        try:
            return await self.coordinator.client.async_image(reading.image_url)
        except VattenKameraError as exc:
            _LOGGER.warning("kunde inte hamta bilden: %s", exc)
            return None
