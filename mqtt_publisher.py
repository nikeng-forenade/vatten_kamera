"""Publicerar det avlasta vardet till Home Assistant over MQTT.

Anvander MQTT-discovery, sa att sensorerna dyker upp i HA automatiskt utan att
nagot behover klistras in i configuration.yaml.

Tva entiteter skapas:
  * sensor  - vardet (liter kvar innan spolning), med detaljer som attribut
  * binary_sensor - huruvida senaste lasningen lyckades
"""

from __future__ import annotations

import json
import logging
import socket
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt

from config import VERSION, MqttConfig
from display_reader import Consensus

log = logging.getLogger(__name__)

DEVICE_NAME = "Vattenkamera (pumpen)"
OBJECT_ID = "vatten_kamera"


class MqttPublisher:
    def __init__(self, cfg: MqttConfig) -> None:
        self.cfg = cfg
        self._client: mqtt.Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.host)

    # --- Anslutning --------------------------------------------------------

    def connect(self) -> bool:
        """Ansluter till broker och publicerar discovery + online."""
        if not self.enabled:
            log.warning("MQTT ar inte konfigurerat - vardet publiceras inte")
            return False

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.cfg.client_id,
            clean_session=True,
        )
        if self.cfg.user:
            client.username_pw_set(self.cfg.user, self.cfg.password)

        availability = self._topic("availability")
        client.will_set(availability, "offline", retain=True)

        try:
            client.connect(self.cfg.host, self.cfg.port, keepalive=30)
        except (OSError, socket.error) as exc:
            log.error("kunde inte ansluta till MQTT-brokern %s:%s (%s)", self.cfg.host, self.cfg.port, exc)
            return False

        client.loop_start()
        self._client = client

        self.publish_discovery()
        client.publish(availability, "online", retain=True)
        log.info("ansluten till MQTT %s:%s", self.cfg.host, self.cfg.port)
        return True

    def disconnect(self) -> None:
        if self._client is None:
            return
        self._client.publish(self._topic("availability"), "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None

    def _topic(self, suffix: str) -> str:
        return f"{self.cfg.base_topic}/{suffix}"

    def _publish(self, topic: str, payload: str, *, retain: bool = True) -> None:
        if self._client is None:
            log.warning("MQTT ar inte anslutet - hoppar over %s", topic)
            return
        self._client.publish(topic, payload, retain=retain)

    # --- Discovery ---------------------------------------------------------

    def _device_block(self) -> dict[str, Any]:
        return {
            "identifiers": [OBJECT_ID],
            "name": DEVICE_NAME,
            "manufacturer": "Hikvision",
            "model": "DS-2CD2432F-IW",
            "sw_version": VERSION,
        }

    def publish_discovery(self) -> None:
        """Publicerar discovery-konfigurationen for vara entiteter."""
        if self._client is None:
            return

        availability = self._topic("availability")

        sensor_config: dict[str, Any] = {
            "name": "Liter kvar innan spolning",
            "unique_id": f"{OBJECT_ID}_liter_kvar",
            "object_id": f"{OBJECT_ID}_liter_kvar",
            "state_topic": self._topic("state"),
            "json_attributes_topic": self._topic("attributes"),
            "unit_of_measurement": "l",
            "icon": "mdi:water",
            "state_class": "measurement",
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self._device_block(),
        }

        status_config: dict[str, Any] = {
            "name": "Senaste lasning lyckades",
            "unique_id": f"{OBJECT_ID}_lasning_ok",
            "object_id": f"{OBJECT_ID}_lasning_ok",
            "state_topic": self._topic("read_ok"),
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self._device_block(),
        }

        prefix = self.cfg.discovery_prefix
        self._publish(f"{prefix}/sensor/{OBJECT_ID}_liter_kvar/config", json.dumps(sensor_config))
        self._publish(f"{prefix}/binary_sensor/{OBJECT_ID}_lasning_ok/config", json.dumps(status_config))
        log.info("discovery publicerad under %s/", prefix)

    # --- Resultat ----------------------------------------------------------

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
        if not self.enabled:
            log.info("MQTT av: varde skulle publicerats = %s", result.value)
            return

        timestamp = read_at or datetime.now()
        numeric = result.numeric

        if numeric is None:
            self.publish_failure("inget varde kunde lasas", read_at=timestamp)
            return

        self._publish(self._topic("state"), str(numeric))

        attributes: dict[str, Any] = {
            "raw_text": result.value,
            "confidence": round(confidence if confidence is not None else result.confidence, 3),
            "röster": f"{result.votes}/{result.total}",
            "read_at": timestamp.isoformat(timespec="seconds"),
        }
        if image:
            attributes["bild"] = image
        if extra:
            attributes.update(extra)

        self._publish(self._topic("attributes"), json.dumps(attributes, ensure_ascii=False))
        self._publish(self._topic("read_ok"), "ON")

        log.info("publicerat varde %s (konfidens %.2f, roster %d/%d)",
                 numeric, confidence if confidence is not None else result.confidence,
                 result.votes, result.total)

    def publish_failure(self, reason: str, *, read_at: datetime | None = None) -> None:
        """Publicerar att lasningen misslyckades."""
        timestamp = read_at or datetime.now()
        self._publish(self._topic("read_ok"), "OFF")
        self._publish(
            self._topic("attributes"),
            json.dumps(
                {
                    "fel": reason,
                    "read_at": timestamp.isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
        )
        log.warning("lasningen misslyckades: %s", reason)

    def publish_diagnostic(self, text: str) -> None:
        """Fri text till en diagnostik-topic, bra vid felsokning."""
        self._publish(self._topic("log"), text)
