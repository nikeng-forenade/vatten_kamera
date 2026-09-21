"""Publicerar vardet direkt till Home Assistants REST-API.

MQTT-brokern behovs alltsa inte: Home Assistant tar emot ett tillstand via sitt
eget API, och programmet har redan en token (samma som lampan anvander). Det har
ar vagen att anvanda nar HA ligger pa en annan maskin an den som laser
displayen, eller nar brokern inte gar att na.

Samma entitetsnamn som MQTT-discovery skapar, sa att sensorerna i Home Assistant
heter likadant oavsett vilken vag vardet kommer in:

  * sensor.vatten_kamera_varde        - vardet, med detaljerna som attribut
  * binary_sensor.vatten_kamera_lasning_ok - huruvida senaste lasningen lyckades

Ett tillstand som satts via API:t forsvinner nar Home Assistant startas om, och
kommer tillbaka vid nasta korning. MQTT-vagen (som ar retainad) gor inte det.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

from config import HaConfig
from display_reader import Consensus

log = logging.getLogger(__name__)

DEVICE_NAME = "Vattenkamera (pumpen)"
OBJECT_ID = "vatten_kamera"

ENTITY_VALUE = f"sensor.{OBJECT_ID}_varde"
ENTITY_OK = f"binary_sensor.{OBJECT_ID}_lasning_ok"


class RestPublisher:
    """Skickar vardet till HA:s API i stallet for till en MQTT-broker."""

    def __init__(
        self,
        cfg: HaConfig,
        *,
        unit: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.cfg = cfg
        self.unit = unit
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.token}",
                "Content-Type": "application/json",
            }
        )
        self._ready = False

    @property
    def enabled(self) -> bool:
        """Har vi adress och token nog for att kunna publicera?"""
        return bool(self.cfg.base_url and self.cfg.token)

    @property
    def connected(self) -> bool:
        return self._ready

    # --- Anslutning --------------------------------------------------------

    def connect(self) -> bool:
        """Kontrollerar att HA svarar och att token duger."""
        if not self.enabled:
            log.warning(
                "HA_BASE_URL eller HA_TOKEN saknas - vardet kan inte publiceras till Home Assistant"
            )
            return False

        try:
            response = self._session.get(f"{self.cfg.base_url}/api/", timeout=self.cfg.timeout_s)
        except requests.RequestException as exc:
            log.error("kunde inte na Home Assistant pa %s: %s", self.cfg.base_url, exc)
            return False

        if response.status_code >= 400:
            log.error(
                "Home Assistant svarade HTTP %s - kontrollera HA_TOKEN",
                response.status_code,
            )
            return False

        self._ready = True
        log.info("ansluten till Home Assistant %s", self.cfg.base_url)
        return True

    def disconnect(self) -> None:
        self._ready = False

    # --- Tillstand ---------------------------------------------------------

    def state_url(self, entity_id: str) -> str:
        return f"{self.cfg.base_url}/api/states/{entity_id}"

    def value_attributes(
        self,
        result: Consensus,
        *,
        confidence: float | None = None,
        read_at: datetime | None = None,
        image: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attributen pa vardesensorn - samma upplysningar som over MQTT."""
        timestamp = read_at or datetime.now()
        attributes: dict[str, Any] = {
            "friendly_name": f"{DEVICE_NAME} - Varde",
            "icon": "mdi:water",
            "state_class": "measurement",
            "raw_text": result.value,
            "confidence": round(
                confidence if confidence is not None else result.confidence, 3
            ),
            "röster": f"{result.votes}/{result.total}",
            "read_at": timestamp.isoformat(timespec="seconds"),
        }
        if self.unit:
            attributes["unit_of_measurement"] = self.unit
        if image:
            attributes["bild"] = image
        if extra:
            attributes.update(extra)
        return attributes

    def publish_result(
        self,
        result: Consensus,
        *,
        confidence: float | None = None,
        read_at: datetime | None = None,
        image: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Publicerar en lyckad lasning."""
        if not self._ready:
            log.warning("inte ansluten till Home Assistant - hoppar over vardet")
            return

        if result.numeric is None:
            self.publish_failure("inget varde kunde lasas", read_at=read_at)
            return

        self._post(
            ENTITY_VALUE,
            f"{result.numeric:.{result.decimals}f}",
            self.value_attributes(
                result, confidence=confidence, read_at=read_at, image=image, extra=extra
            ),
        )
        self._post(ENTITY_OK, "ON", self._ok_attributes("senaste lasningen lyckades"))
        log.info(
            "publicerat varde %s till Home Assistant (konfidens %.2f, roster %d/%d)",
            result.numeric,
            confidence if confidence is not None else result.confidence,
            result.votes,
            result.total,
        )

    def publish_failure(self, reason: str, *, read_at: datetime | None = None) -> None:
        """Publicerar att lasningen misslyckades."""
        if not self._ready:
            return

        self._post(ENTITY_OK, "OFF", self._ok_attributes(reason))
        log.warning("lasningen misslyckades: %s", reason)

    def publish_test(self, value: float, *, unit: str = "") -> None:
        """Lagger in ett provvarde, sa att sensorerna syns i HA med en gang."""
        self._post(
            ENTITY_VALUE,
            f"{value:g}",
            {
                "friendly_name": f"{DEVICE_NAME} - Varde",
                "icon": "mdi:water",
                "raw_text": "provvarde",
                "confidence": 1.0,
                "read_at": datetime.now().isoformat(timespec="seconds"),
                "unit_of_measurement": unit or self.unit,
            },
        )
        self._post(ENTITY_OK, "ON", self._ok_attributes("provvarde"))

    def _ok_attributes(self, text: str) -> dict[str, Any]:
        return {
            "friendly_name": f"{DEVICE_NAME} - Senaste lasning lyckades",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "text": text,
        }

    def _post(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        try:
            response = self._session.post(
                self.state_url(entity_id),
                json={"state": state, "attributes": attributes},
                timeout=self.cfg.timeout_s,
            )
        except requests.RequestException as exc:
            log.error("kunde inte skicka %s till Home Assistant: %s", entity_id, exc)
            return

        if response.status_code >= 400:
            log.error(
                "Home Assistant nekade %s: HTTP %s %s",
                entity_id,
                response.status_code,
                response.text[:200],
            )
            return

        log.debug("%s = %s (HTTP %s)", entity_id, state, response.status_code)
