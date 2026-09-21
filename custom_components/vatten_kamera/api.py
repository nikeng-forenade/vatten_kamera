"""HTTP-klient mot vardetjansten."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class VattenKameraError(Exception):
    """Kunde inte na eller forsta vardetjansten."""


class VattenKameraClient:
    """Laser senaste vardet ur vardetjanstens lilla HTTP-API."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self.host = host
        self.port = port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def absolute_url(self, path: str | None) -> str | None:
        """Gor '/api/frames/...' ur svaret om till en full adress."""
        if not path:
            return None
        if path.startswith("http"):
            return path
        return f"{self.base_url}{path}"

    async def async_latest(self) -> dict[str, Any]:
        """Senaste lasningen."""
        return await self._request("GET", "/api/latest")

    async def async_health(self) -> dict[str, Any]:
        """Tjanstens lage: version, om en lasning paga, nasta korning."""
        return await self._request("GET", "/api/health")

    async def async_start_run(self) -> dict[str, Any]:
        """Ber tjansten lasa displayen nu (knappen i Home Assistant)."""
        return await self._request("POST", "/api/run")

    async def async_image(self, url: str) -> bytes | None:
        """Hamtar sjalva bilden (beviset) som en lasning bygger pa."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.get(url, timeout=timeout) as response:
                if response.status != 200:
                    raise VattenKameraError(f"{url} svarade {response.status}")
                return await response.read()
        except aiohttp.ClientError as exc:
            raise VattenKameraError(f"kunde inte hamta {url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise VattenKameraError(f"{url} svarade inte inom {REQUEST_TIMEOUT} s") from exc

    async def _request(self, method: str, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.request(method, url, timeout=timeout) as response:
                try:
                    data = await response.json(content_type=None)
                except ValueError as exc:
                    raise VattenKameraError(f"{url} svarade inte med JSON ({exc})") from exc
                if response.status != 200:
                    text = ""
                    if isinstance(data, dict):
                        text = str(data.get("text") or data.get("error") or "")
                    raise VattenKameraError(
                        f"{url} svarade {response.status}" + (f": {text}" if text else "")
                    )
                if not isinstance(data, dict):
                    raise VattenKameraError(f"{url} svarade inte med ett objekt")
                return data
        except aiohttp.ClientError as exc:
            raise VattenKameraError(f"kunde inte na {url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise VattenKameraError(f"{url} svarade inte inom {REQUEST_TIMEOUT} s") from exc
