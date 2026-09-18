"""Klient mot Home Assistant.

Anvands for att tanda/slacka lampan vid pumpen och for att skicka en notis om
en lasning misslyckas. Allt gar over HA:s vanliga REST-API med en langlivad
token, sa ingen egen integration behover installeras.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import HaConfig

log = logging.getLogger(__name__)


class HaError(RuntimeError):
    """Home Assistant svarade med ett fel."""


class HomeAssistant:
    def __init__(self, cfg: HaConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.token}",
                "Content-Type": "application/json",
            }
        )

    @property
    def configured(self) -> bool:
        return self.cfg.enabled

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url}/api{path}"

    def _check(self) -> None:
        if not self.cfg.enabled:
            raise HaError("HA_BASE_URL eller HA_TOKEN saknas i .env")

    def call_service(self, domain: str, service: str, **data: Any) -> Any:
        """Anropar en tjans i HA, t.ex. light.turn_on."""
        self._check()
        url = self._url(f"/services/{domain}/{service}")
        try:
            response = self._session.post(url, json=data, timeout=self.cfg.timeout_s)
        except requests.RequestException as exc:
            raise HaError(f"kunde inte na Home Assistant: {exc}") from exc

        if response.status_code >= 400:
            raise HaError(
                f"{domain}.{service} misslyckades: HTTP {response.status_code} {response.text[:200]}"
            )

        log.debug("%s.%s %s -> HTTP %s", domain, service, data, response.status_code)
        return response.json() if response.content else None

    def get_state(self, entity_id: str) -> dict[str, Any]:
        """Hamtar aktuellt tillstand for en entitet."""
        self._check()
        try:
            response = self._session.get(
                self._url(f"/states/{entity_id}"), timeout=self.cfg.timeout_s
            )
        except requests.RequestException as exc:
            raise HaError(f"kunde inte na Home Assistant: {exc}") from exc

        if response.status_code == 404:
            raise HaError(f"entiteten {entity_id} finns inte i Home Assistant")
        if response.status_code >= 400:
            raise HaError(f"HTTP {response.status_code} for {entity_id}")
        return response.json()

    # --- Lampa -------------------------------------------------------------

    @property
    def has_lamp(self) -> bool:
        return bool(self.cfg.light_entity)

    def lamp_on(self, *, brightness_pct: int | None = None) -> bool:
        """Tander lampan. Returnerar False om ingen lampa ar konfigurerad."""
        if not self.has_lamp:
            log.warning("ingen lampa konfigurerad (HA_LIGHT_ENTITY) - hoppar over att tanda")
            return False

        entity = self.cfg.light_entity
        domain = entity.split(".")[0]
        data: dict[str, Any] = {"entity_id": entity}
        if brightness_pct is not None and domain == "light":
            data["brightness_pct"] = brightness_pct

        self.call_service(domain, "turn_on", **data)
        log.info("lampa tand: %s", entity)
        return True

    def lamp_off(self) -> bool:
        """Slacker lampan. Returnerar False om ingen lampa ar konfigurerad."""
        if not self.has_lamp:
            return False

        entity = self.cfg.light_entity
        domain = entity.split(".")[0]
        self.call_service(domain, "turn_off", entity_id=entity)
        log.info("lampa slackt: %s", entity)
        return True

    def notify(self, message: str, *, title: str = "Vattenkamera") -> None:
        """Skickar en notis via HA:s notify-tjanst om en sadan finns."""
        self._check()
        try:
            self.call_service("persistent_notification", "create", title=title, message=message)
        except HaError as exc:
            log.warning("kunde inte skicka notis: %s", exc)
