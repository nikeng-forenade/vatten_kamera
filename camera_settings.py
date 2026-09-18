"""Laser och andrar kamerans bildinstallningar via ISAPI.

Siffrorna pa displayen ar en liten, starkt sjalvlysande yta pa en mork botten.
Med hog forstarkning, lang slutartid och avstangt overexponeringsskydd branner
den ut och sprids till en glo runt siffrorna - da tappar avlasningen kanterna.

Allt som andras backas upp forst, sa det gar att aterstalla.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
from requests.auth import HTTPBasicAuth

from config import ROOT, CameraConfig

log = logging.getLogger(__name__)

IMAGE_URL = "/ISAPI/Image/channels/1"
BACKUP_FILE = ROOT / "camera_settings_backup.json"
# Det kameralage som anvands under sjalva lasningen. Kameran lanas till detta
# lage strax innan och laggs tillbaka direkt efterat, sa att den inte lamnas i
# ett lage som gor bilden mork for allt annat.
PROFILE_FILE = ROOT / "camera_profile.json"

# Etikett -> regex som pekar ut vardet i XML:en.
# Taggnamnen ar inte unika i dokumentet (t.ex. <mode> finns pa flera stallen),
# darfor ankras de dar sa behovs.
PATTERNS: dict[str, str] = {
    "ircut": r"(<IrcutFilterType>)([^<]*)(</IrcutFilterType>)",
    "exposure_type": r"(<ExposureType>)([^<]*)(</ExposureType>)",
    "gain": r"(<GainLevel>)([^<]*)(</GainLevel>)",
    "shutter": r"(<ShutterLevel>)([^<]*)(</ShutterLevel>)",
    "sharpness": r"(<SharpnessLevel>)([^<]*)(</SharpnessLevel>)",
    # Ankras till Color-blocket: <brightnessLevel> finns ocksa under LaserLight.
    "brightness": r"(<Color[^>]*>.*?<brightnessLevel>)([^<]*)(</brightnessLevel>)",
    "contrast": r"(<Color[^>]*>.*?<contrastLevel>)([^<]*)(</contrastLevel>)",
    "saturation": r"(<Color[^>]*>.*?<saturationLevel>)([^<]*)(</saturationLevel>)",
    "wdr_mode": r"(<WDR[^>]*>\s*<mode>)([^<]*)(</mode>)",
    "wdr_level": r"(<WDRLevel>)([^<]*)(</WDRLevel>)",
    "noise_reduce": r"(<NoiseReduce[^>]*>\s*<mode>)([^<]*)(</mode>)",
    "noise_reduce_level": r"(<generalLevel>)([^<]*)(</generalLevel>)",
    "overexpose_suppress": (
        r"(<OverexposeSuppress[^>]*>\s*<enabled>)([^<]*)(</enabled>)"
    ),
}

# Rimliga varden, sa vi inte skickar nagot kameran inte forstar.
ALLOWED: dict[str, tuple[str, ...] | None] = {
    "ircut": ("auto", "day", "night"),
    "exposure_type": ("auto", "manual"),
    "wdr_mode": ("open", "close"),
    "noise_reduce": ("close", "general", "advanced"),
    "overexpose_suppress": ("true", "false"),
    "gain": None,
    "shutter": None,
    "sharpness": None,
    "brightness": None,
    "contrast": None,
    "saturation": None,
    "wdr_level": None,
    "noise_reduce_level": None,
}


class CameraSettingsError(RuntimeError):
    pass


@dataclass
class CameraSettings:
    cfg: CameraConfig
    xml: str = ""
    values: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(self.cfg.user, self.cfg.password)

    def read(self) -> dict[str, str]:
        """Hamtar aktuella installningar."""
        url = f"{self.cfg.base_url}{IMAGE_URL}"
        try:
            response = self._session.get(url, timeout=self.cfg.timeout_s)
        except requests.RequestException as exc:
            raise CameraSettingsError(f"kunde inte na kameran: {exc}") from exc
        if response.status_code != 200:
            raise CameraSettingsError(f"HTTP {response.status_code} vid lasning av bildinstallningar")

        self.xml = response.text
        self.values = self.parse(self.xml)
        return self.values

    @staticmethod
    def parse(xml: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, pattern in PATTERNS.items():
            match = re.search(pattern, xml, re.DOTALL)
            if match:
                values[key] = match.group(2).strip()
        return values

    def backup(self, *, force: bool = False) -> Path:
        """Sparar nuvarande installningar sa de kan aterstallas.

        Skriver aldrig over en befintlig backup utan tvingas. En backup som
        skrivs over ar vardelos, och da gar det inte att komma tillbaka till
        utgangslaget - vilket hande en gang och gjorde att kamerans eget
        nattlage inte gick att fa tillbaka.
        """
        if BACKUP_FILE.exists() and not force:
            log.info("backup finns redan (%s) - ror den inte", BACKUP_FILE.name)
            return BACKUP_FILE

        if not self.xml:
            self.read()
        BACKUP_FILE.write_text(
            json.dumps(
                {
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                    "camera": self.cfg.ip,
                    "values": self.values,
                    "xml": self.xml,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log.info("bildinstallningar backade upp -> %s", BACKUP_FILE.name)
        return BACKUP_FILE

    # --- Spara och lagga tillbaka lage runt en korning ---------------------

    def capture(self) -> str:
        """Kamerans installningar som XML, for att kunna lagga tillbaka dem."""
        if not self.xml:
            self.read()
        return self.xml

    def push(self, xml: str) -> dict[str, str]:
        """Lagger tillbaka ett tidigare sparat XML-dokument."""
        self.put_xml(xml)
        self.read()
        return self.values

    def save_profile(self, settings: dict[str, str] | None = None) -> Path:
        """Sparar det kameralage som ska anvandas under sjalva lasningen.

        Kameran rors inte - bara filen skrivs. Det gor att man kan bestamma
        laslaget utan att forst behova stalla om kameran.
        """
        values = dict(settings or self.read())

        unknown = [key for key in values if key not in PATTERNS]
        if unknown:
            raise CameraSettingsError(
                f"okanda installningar: {', '.join(unknown)}. Giltiga: {', '.join(PATTERNS)}"
            )
        for key, value in values.items():
            allowed = ALLOWED.get(key)
            if allowed and value not in allowed:
                raise CameraSettingsError(f"{key} maste vara en av {', '.join(allowed)}")

        PROFILE_FILE.write_text(
            json.dumps({"settings": values}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        log.info("lasprofil sparad -> %s", PROFILE_FILE.name)
        return PROFILE_FILE

    def load_profile(self) -> dict[str, str] | None:
        """Laser lasprofilen, om det finns nagon."""
        if not PROFILE_FILE.exists():
            return None
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("kunde inte lasa %s: %s", PROFILE_FILE.name, exc)
            return None
        settings = data.get("settings")
        return settings if isinstance(settings, dict) and settings else None


    def put_xml(self, xml: str) -> None:
        """Skickar ett helt XML-dokument till kameran."""
        url = f"{self.cfg.base_url}{IMAGE_URL}"
        try:
            response = self._session.put(
                url,
                data=xml.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=self.cfg.timeout_s,
            )
        except requests.RequestException as exc:
            raise CameraSettingsError(f"kunde inte na kameran: {exc}") from exc

        if response.status_code not in (200, 201, 204):
            raise CameraSettingsError(
                f"kameran avvisade andringen: HTTP {response.status_code} {response.text[:300]}"
            )

    def apply(self, changes: dict[str, str]) -> dict[str, str]:
        """Andrar angivna installningar och verifierar resultatet."""
        if not self.xml:
            self.read()

        unknown = [key for key in changes if key not in PATTERNS]
        if unknown:
            raise CameraSettingsError(
                f"okanda installningar: {', '.join(unknown)}. Giltiga: {', '.join(PATTERNS)}"
            )

        for key, value in changes.items():
            allowed = ALLOWED.get(key)
            if allowed and value not in allowed:
                raise CameraSettingsError(f"{key} maste vara en av {', '.join(allowed)}")

        updated = self.xml
        for key, value in changes.items():
            pattern = PATTERNS[key]
            if not re.search(pattern, updated, re.DOTALL):
                raise CameraSettingsError(f"hittade inte {key} i kamerans XML")
            updated = re.sub(pattern, lambda m: m.group(1) + value + m.group(3), updated, count=1, flags=re.DOTALL)

        self.put_xml(updated)

        # Las om och kontrollera vad som faktiskt galler nu.
        self.read()
        return self.values

    def restore(self) -> dict[str, str]:
        """Aterstaller installningarna fran backupen."""
        if not BACKUP_FILE.exists():
            raise CameraSettingsError(f"ingen backup finns ({BACKUP_FILE.name})")

        data = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
        original_xml = data.get("xml", "")
        if not original_xml:
            raise CameraSettingsError("backupen innehaller ingen XML")

        self.put_xml(original_xml)
        self.read()
        return self.values


def display_quality(gray: np.ndarray) -> tuple[float, float]:
    """Mat pa hur bra displayen ar atergiven. Hogre ar battre.

    En bra bild av en sjusegmentsdisplay ar tydligt tudelad: siffrorna ar ljusa
    och bakgrunden mork, utan mellantoner. Vi mater darfor hur val en troskel
    kan skilja de tva grupperna at (Otsus mellanklassvarians) och straffar
    bilder dar siffrorna branner ut.

    Returnerar (poang 0-1, andel utbranda pixlar).
    """
    values = np.clip(gray, 0, 255).astype(np.uint8)
    histogram = np.bincount(values.ravel(), minlength=256).astype(np.float64)
    total = histogram.sum()
    if total <= 0:
        return 0.0, 0.0

    probability = histogram / total
    levels = np.arange(256, dtype=np.float64)
    omega = np.cumsum(probability)
    mu = np.cumsum(probability * levels)
    mu_total = mu[-1]

    denominator = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = np.where(
            denominator > 1e-9,
            (mu_total * omega - mu) ** 2 / denominator,
            0.0,
        )

    score = float(between.max()) / (255.0**2)
    clipped = float(probability[254:].sum())

    # Utan tta siffror finns ingen kontrast kvar att tolka.
    if clipped > 0.05:
        score *= max(0.0, 1.0 - (clipped - 0.05) / 0.05)

    return score, clipped
