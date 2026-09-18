"""Kameraklient for Hikvision DS-2CD2432F-IW.

Kameran svarar pa Basic auth over ISAPI (Digest ger 401 pa denna firmware).
Snapshot via /ISAPI/Streaming/channels/<kanal>/picture tar under en sekund
och ger 2048x1536 px - bra nog att lasa en display pa nara hall.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2
import numpy as np
import requests
from requests.auth import HTTPBasicAuth

from config import CameraConfig

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Kameran svarade inte som vantat."""


@dataclass
class Frame:
    """En tagen bild med metadata."""

    image: np.ndarray  # BGR
    timestamp: float  # unix-tid
    jpeg: bytes = b""

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


class HikvisionCamera:
    """Tunn klient mot kamerans snapshot-API."""

    def __init__(self, cfg: CameraConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(cfg.user, cfg.password)

    def device_info(self) -> str:
        """Returnerar kamerans deviceInfo-XML. Kastar CameraError vid fel."""
        return self._get_text("/ISAPI/System/deviceInfo")

    def _get_text(self, path: str) -> str:
        url = f"{self.cfg.base_url}{path}"
        try:
            r = self._session.get(url, timeout=self.cfg.timeout_s)
        except requests.RequestException as exc:
            raise CameraError(f"kunde inte na kameran: {exc}") from exc
        if r.status_code != 200:
            raise CameraError(f"HTTP {r.status_code} for {path}")
        return r.text

    def snapshot(self) -> Frame:
        """Hamtar en JPEG-snapshot och avkodar den till en bild."""
        last_error: Exception | None = None

        for attempt in range(1, self.cfg.retries + 2):
            try:
                r = self._session.get(self.cfg.snapshot_url, timeout=self.cfg.timeout_s)
                if r.status_code != 200:
                    raise CameraError(f"HTTP {r.status_code} vid snapshot")
                if r.content[:2] != b"\xff\xd8":
                    raise CameraError("svaret var ingen JPEG")

                img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    raise CameraError("kunde inte avkoda JPEG")

                return Frame(image=img, timestamp=time.time(), jpeg=r.content)

            except (CameraError, requests.RequestException) as exc:
                last_error = exc
                if attempt <= self.cfg.retries:
                    log.warning("snapshot forsok %d misslyckades (%s), forsoker igen", attempt, exc)
                    time.sleep(0.5 * attempt)

        raise CameraError(f"snapshot misslyckades efter {self.cfg.retries + 1} forsok: {last_error}")

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> HikvisionCamera:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def main() -> int:
    """Enkel självtest: python camera.py"""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from config import load_config

    cfg = load_config()
    cam = HikvisionCamera(cfg.camera)
    try:
        print(cam.device_info()[:400])
        frame = cam.snapshot()
        print(f"snapshot: {frame.width}x{frame.height}px, {len(frame.jpeg)//1024} kB")
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        cam.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
