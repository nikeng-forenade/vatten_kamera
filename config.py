"""Konfiguration for vatten_kamera.

Alla hemligheter och maskinvaruspecifika varden las fran .env.
Ovriga installningar har vettiga defaultvarden och kan overridas via .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Bumpas vid varje andring sa vi har koll pa vad som kor pa servern.
VERSION = "0.9.0"

ROOT = Path(__file__).resolve().parent
CALIBRATION_FILE = ROOT / "calibration.json"

load_dotenv(ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _get_float(key: str, default: float) -> float:
    raw = _get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = _get(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "ja", "on"}


@dataclass(frozen=True)
class CameraConfig:
    """Hikvision DS-2CD2432F-IW. Basic auth pa ISAPI."""

    ip: str
    user: str
    password: str
    http_port: int = 80
    rtsp_port: int = 554
    channel: str = "101"
    timeout_s: float = 20.0
    retries: int = 2

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.http_port}"

    @property
    def snapshot_url(self) -> str:
        return f"{self.base_url}/ISAPI/Streaming/channels/{self.channel}/picture"

    @property
    def rtsp_url(self) -> str:
        path = _get("CAMERA_RTSP_PATH", "/Streaming/Channels/1")
        return f"rtsp://{self.user}:{self.password}@{self.ip}:{self.rtsp_port}{path}"


@dataclass(frozen=True)
class HaConfig:
    """Home Assistant via REST-API (langlivad token)."""

    base_url: str
    token: str
    light_entity: str = ""
    timeout_s: float = 10.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    user: str = ""
    password: str = ""
    base_topic: str = "vatten_kamera"
    # Enhet for sensorn i Home Assistant.
    unit: str = "l"
    client_id: str = "vatten_kamera"
    discovery_prefix: str = "homeassistant"
    enabled: bool = False


@dataclass(frozen=True)
class RunConfig:
    """Nar och hur lange vi laser displayen."""

    # Klockslaget da vardet dyker upp (lokal tid).
    run_at: str = "02:00:00"
    # Hur lange innan vi startar (lamppavarmning, kamerans exponering).
    pre_start_s: float = 8.0
    # Hur lange efter run_at vi fortsatter lasa.
    window_s: float = 25.0
    # Tid mellan bilderna.
    interval_s: float = 1.0
    # Vanta sa har lange efter att lampan tands innan forsta bilden.
    lamp_warmup_s: float = 2.0
    # Vanta sa har lange efter sista bilden innan lampan slacks.
    lamp_cooldown_s: float = 1.0
    # Spara bilderna fran korningen (bevis vid felsokning).
    save_frames: bool = True
    # Kameran lanas till lasprofilen (camera_profile.json) strax innan lasningen
    # och laggs tillbaka direkt efter, sa att den inte lamnas i ett morkt lage.
    use_camera_profile: bool = True
    # Spara bara bilder dar ett varde kunde lasas.
    save_only_success: bool = False
    # Antal bilder som maste vara eniga for att vardet ska publiceras.
    min_agreement: int = 3
    # Minsta konfidens per siffra (0-1) for att en lasning ska raknas.
    min_confidence: float = 0.75
    # Tva bilder som visar samma varde laggs ihop innan tolkning. Detta ar den
    # storsta tillatna medelskillnaden mellan dem (0-255). Brus ligger lagt,
    # ett vardebyte ligger hogt.
    group_threshold: float = 8.0
    # Sand notis till HA om lasningen misslyckas.
    notify_on_failure: bool = False


@dataclass(frozen=True)
class ReaderConfig:
    """Bildbehandling for displayen."""

    # Antal tecken i displayen (siffror + ev. separatorer).
    # 5 = t.ex. "1050" med en separat decimalpunkt, se calibration.json.
    digit_count: int = 4
    # Antal decimaler i vardet. Displayen visar t.ex. 1.22 och 0.50, alltsa tre
    # siffror dar de tva sista ar decimaler. 0 = vardet ar ett heltal.
    decimals: int = 0
    # Kraver att alla sifferpositioner lyser. Visar displayen alltid ledande
    # nolla (som i 0.50) sa betyder en slackt siffra att tolkningen hamnat fel.
    require_all_digits: bool = True
    require_blank_first: bool = False
    reject_all_eights: bool = False
    reference_file: str = ""
    # Vilken fargkanal som blir graaskala: "auto", "gray", "r", "g" eller "b".
    # En rod LED-display lyser starkast i rodkanalen, sa "auto" valjer den kanal
    # som har storst kontrast - det ger flera ganger battre skillnad an graaskala.
    channel: str = "auto"
    # Forstoring innan troskling - sma siffror behover mer pixlar.
    upscale: float = 5.0
    # Normalisera ljusstyrkan innan troskling (klarar svagt ljus battre).
    normalize: bool = True
    # Siffrorna ar ljusa pa mork botten.
    invert: bool = False
    # 0 = Otsu, annars fast troskel.
    threshold: int = 0
    # Streck ut siffrorna sa glapp i segmenten tats.
    close_kernel: int = 3
    # Ta bort den spegelvanda reflektionen i displayglaset.
    clip_bottom: float = 0.0


@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    ha: HaConfig
    mqtt: MqttConfig
    run: RunConfig
    reader: ReaderConfig
    calibration_file: Path = field(default=CALIBRATION_FILE)
    # Utsnitt for displayen, t.ex. "430,1185,590,1240" (x1,y1,x2,y2).
    # Satts i .env sa att kommandon slipper skicka med argument.
    calibration_roi: tuple[int, int, int, int] | None = None


def load_config() -> Config:
    camera = CameraConfig(
        ip=_get("CAMERA_IP", "192.168.1.213"),
        user=_get("CAMERA_USER", "admin"),
        password=_get("CAMERA_PASSWORD"),
        http_port=_get_int("CAMERA_HTTP_PORT", 80),
        rtsp_port=_get_int("CAMERA_RTSP_PORT", 554),
        channel=_get("CAMERA_CHANNEL", "101"),
        timeout_s=_get_float("CAMERA_TIMEOUT_S", 20.0),
        retries=_get_int("CAMERA_RETRIES", 2),
    )

    ha = HaConfig(
        base_url=_get("HA_BASE_URL", "").rstrip("/"),
        token=_get("HA_TOKEN"),
        light_entity=_get("HA_LIGHT_ENTITY"),
        timeout_s=_get_float("HA_TIMEOUT_S", 10.0),
    )

    mqtt = MqttConfig(
        host=_get("MQTT_HOST", ""),
        port=_get_int("MQTT_PORT", 1883),
        user=_get("MQTT_USER"),
        password=_get("MQTT_PASSWORD"),
        base_topic=_get("MQTT_BASE_TOPIC", "vatten_kamera"),
        unit=_get("UNIT", "l"),
        client_id=_get("MQTT_CLIENT_ID", "vatten_kamera"),
        discovery_prefix=_get("MQTT_DISCOVERY_PREFIX", "homeassistant"),
        enabled=_get_bool("MQTT_ENABLED", bool(_get("MQTT_HOST"))),
    )

    run = RunConfig(
        run_at=_get("RUN_AT", "02:00:00"),
        pre_start_s=_get_float("PRE_START_S", 8.0),
        window_s=_get_float("WINDOW_S", 25.0),
        interval_s=_get_float("INTERVAL_S", 1.0),
        lamp_warmup_s=_get_float("LAMP_WARMUP_S", 2.0),
        lamp_cooldown_s=_get_float("LAMP_COOLDOWN_S", 1.0),
        save_frames=_get_bool("SAVE_FRAMES", True),
        use_camera_profile=_get_bool("USE_CAMERA_PROFILE", True),
        save_only_success=_get_bool("SAVE_ONLY_SUCCESS", False),
        min_agreement=_get_int("MIN_AGREEMENT", 3),
        min_confidence=_get_float("MIN_CONFIDENCE", 0.75),
        group_threshold=_get_float("GROUP_THRESHOLD", 8.0),
        notify_on_failure=_get_bool("NOTIFY_ON_FAILURE", False),
    )

    reader = ReaderConfig(
        digit_count=_get_int("DIGIT_COUNT", 4),
        decimals=_get_int("DECIMALS", 0),
        require_all_digits=_get_bool("REQUIRE_ALL_DIGITS", True),
        require_blank_first=_get_bool("REQUIRE_BLANK_FIRST", False),
        reject_all_eights=_get_bool("REJECT_ALL_EIGHTS", False),
        reference_file=_get("REFERENCE_FILE", ""),
        channel=_get("COLOR_CHANNEL", "auto"),
        upscale=_get_float("UPSCALE", 5.0),
        normalize=_get_bool("NORMALIZE", True),
        invert=_get_bool("INVERT", False),
        threshold=_get_int("THRESHOLD", 0),
        close_kernel=_get_int("CLOSE_KERNEL", 3),
        clip_bottom=_get_float("CLIP_BOTTOM", 0.0),
    )

    return Config(camera=camera, ha=ha, mqtt=mqtt, run=run, reader=reader,
                  calibration_roi=_parse_roi(_get("CALIBRATION_ROI")))


def _parse_roi(raw: str) -> tuple[int, int, int, int] | None:
    """Laser 'x1,y1,x2,y2' ur .env."""
    if not raw:
        return None
    try:
        parts = [int(float(part)) for part in raw.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]
