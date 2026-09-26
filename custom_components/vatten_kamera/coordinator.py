"""Hamta senaste vardet med jamna mellanrum."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import VattenKameraClient, VattenKameraError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def as_datetime(text: Any) -> datetime | None:
    """Tolkar tidsstampeln ur svaret, med eller utan tidszon."""
    if not isinstance(text, str) or not text:
        return None
    moment = dt_util.parse_datetime(text)
    if moment is None:
        return None
    if moment.tzinfo is None:
        # Tjansten skriver lokal tid utan tidszon - tolka den som lokal tid har.
        moment = moment.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_local(moment)


@dataclass(frozen=True)
class Reading:
    """En lasning ur vardetjansten, med tolkade falt."""

    ok: bool
    value: str | None
    numeric: float | None
    display: str | None
    unit: str
    decimals: int
    confidence: float
    votes: int
    frames: int
    read_at: datetime | None
    image_url: str | None
    version: str
    error: str
    published_to: str
    # Flodet just nu - sidan efter vardet i varvet. Det sager om vatten rinner,
    # och ar underlaget for lackagelarmet.
    flow: str | None = None
    flow_numeric: float | None = None
    flow_unit: str = ""
    flow_note: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any], image_url: str | None) -> "Reading":
        """Bygger en lasning av svaret fran /api/latest."""
        return cls(
            ok=bool(payload.get("ok")),
            value=payload.get("value"),
            numeric=_as_float(payload.get("numeric")),
            display=payload.get("display"),
            unit=str(payload.get("unit") or ""),
            decimals=_as_int(payload.get("decimals")) or 2,
            confidence=_as_float(payload.get("confidence")) or 0.0,
            votes=_as_int(payload.get("votes")),
            frames=_as_int(payload.get("frames")),
            read_at=as_datetime(payload.get("read_at_iso") or payload.get("read_at")),
            image_url=image_url,
            version=str(payload.get("version") or ""),
            error=str(payload.get("error") or ""),
            published_to=str(payload.get("published_to") or ""),
            flow=payload.get("flow"),
            flow_numeric=_as_float(payload.get("flow_numeric")),
            flow_unit=str(payload.get("flow_unit") or ""),
            flow_note=str(payload.get("flow_note") or ""),
        )


class VattenKameraCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Haller senaste vardet uppdaterat.

    Tva anrop per omgang: /api/latest (vardet) och /api/health (om tjansten
    lever och nar nasta lasning sker). Gar det inte att na tjansten alls blir
    entiteterna otillgangliga i stallet for att visa ett gammalt varde.
    """

    def __init__(self, hass: HomeAssistant, client: VattenKameraClient, scan_interval: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(10, scan_interval)),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            latest = await self.client.async_latest()
        except VattenKameraError as exc:
            raise UpdateFailed(str(exc)) from exc

        health: dict[str, Any] = {}
        try:
            health = await self.client.async_health()
        except VattenKameraError as exc:
            # Vardet kom fram men tjansten svarar inte pa allt - det ar inget
            # skal att gora alla entiteter otillgangliga.
            _LOGGER.debug("halsa kunde inte lasas: %s", exc)

        return {
            "latest": latest,
            "health": health,
            "reading": Reading.from_payload(
                latest, self.client.absolute_url(latest.get("bild_url"))
            ),
        }

    @property
    def reading(self) -> Reading | None:
        return (self.data or {}).get("reading")

    @property
    def health(self) -> dict[str, Any]:
        return (self.data or {}).get("health") or {}
