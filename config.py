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
VERSION = "0.10.0"

ROOT = Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# Var filerna som hor till installationen bor: kalibreringen, lasprofilen och
# bilderna fran varje korning. Satts till /data nar programmet kor som tillagg i
# Home Assistant, sa att de overlever en uppdatering av tillagget.
DATA_DIR = Path(_get("DATA_DIR") or ROOT)
CAPTURES_DIR = Path(_get("CAPTURES_DIR") or DATA_DIR / "captures")
CALIBRATION_FILE = Path(_get("CALIBRATION_FILE") or DATA_DIR / "calibration.json")


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


# Den lilla HTTP-tjansten som sager vad senaste lasningen gav. Ingen grafik -
# bara JSON, sa att Home Assistant (och du sjalv med curl) kan se vardet utan att
# ga via en MQTT-broker. 0 = tjansten ar av.
STATUS_PORT = _get_int("STATUS_PORT", 0)
STATUS_BIND = _get("STATUS_BIND") or "0.0.0.0"
# Live-vyn i granssnittet: ska sidan uppdatera sig sjalv, eller bara nar man ber
# om det? Gar att stanga av bade i granssnittet och har.
STATUS_LIVE = _get_bool("STATUS_LIVE", True)
# Far man starta en lasning respektive starta om tjansten fran granssnittet?
STATUS_ALLOW_RUN = _get_bool("STATUS_ALLOW_RUN", True)
STATUS_ALLOW_RESTART = _get_bool("STATUS_ALLOW_RESTART", True)

# Pausen mellan tva lasningar nar MODE=intervall och EVERY_MINUTES=0 ("hela
# tiden"). En lasning tar en dryg minut anda, sa pausen ar bara sa att kameran
# hinner stalla in sig och loggen gar att lasa.
INTERVAL_PAUSE_S = 5.0
# Senaste lasningen, sa att svaret finns kvar aven efter en omstart.
LATEST_FILE = Path(_get("LATEST_FILE") or DATA_DIR / "latest.json")
# Loggfilen som granssnittet visar de sista raderna ur.
LOG_FILE = Path(_get("LOG_FILE") or DATA_DIR / "vatten_kamera.log")


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

    # Hur lasningen startas:
    #   intervall - laser hela tiden, en lasning var EVERY_MINUTES minut
    #               (0 = direkt efter den forra, alltsa i praktiken hela tiden)
    #   manuell   - bara nar du sjalv trycker "Las nu" (granssnittet eller
    #               knappen i Home Assistant)
    #   natt      - en gang per dygn, strax innan RUN_AT
    #
    # Varje lasning tittar pa displayen tills den visar spolttiden 02:00 och tar
    # vardet fran sidan efter den - sa klockslaget behovs egentligen bara for att
    # slippa lasa i onodan.
    mode: str = "intervall"
    # Hur ofta vi laser i lage 'intervall'. 0 = sa snart den forra ar klar.
    every_minutes: float = 5.0
    # Klockslaget da vardet dyker upp (lokal tid).
    run_at: str = "02:00:00"
    # Hur lange innan vi startar. Marginalen ar till for att korningen ska hinna
    # borja titta pa displayen innan den visar spolttiden (02:00). Vi vet anda
    # inte exakt nar - pumpens klocka gar efter - sa vi tittar tills vi ser den.
    pre_start_s: float = 300.0
    # Hur lange vi tittar som mest innan vi ger upp (RUN_AT + WINDOW_S).
    window_s: float = 900.0
    # Sluta titta sa snart displayen visat spolttiden och vardesidan efter den.
    stop_when_ready: bool = True
    # Tid mellan bilderna. Tva sekunder ar for glest: vardesidan star stilla i
    # 10-12 s, och da hinner det bara bli nagra bilder att bygga en rost pa.
    interval_s: float = 1.5
    # Vanta sa har lange efter att lampan tands innan forsta bilden.
    lamp_warmup_s: float = 2.0
    # Vanta sa har lange efter sista bilden innan lampan slacks.
    lamp_cooldown_s: float = 1.0
    # Spara bilderna fran korningen (bevis vid felsokning).
    save_frames: bool = True
    # Kameran lanas till lasprofilen (camera_profile.json) strax innan lasningen
    # och laggs tillbaka direkt efter, sa att den inte lamnas i ett morkt lage.
    use_camera_profile: bool = True
    # Var vardet publiceras. HA kan ligga pa en annan maskin an den har, och da
    # behovs ingen MQTT-broker alls: vardet kan ga rakt in i HA:s eget API.
    #   auto  - MQTT om brokern svarar, annars HA:s API
    #   mqtt  - bara MQTT
    #   rest  - bara HA:s API (kraver HA_BASE_URL och HA_TOKEN)
    #   bada  - till bada
    publish_to: str = "auto"
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
        mode=_read_mode(_get("MODE", "intervall")),
        every_minutes=_get_float("EVERY_MINUTES", 5.0),
        run_at=_get("RUN_AT", "02:00:00"),
        pre_start_s=_get_float("PRE_START_S", 300.0),
        window_s=_get_float("WINDOW_S", 900.0),
        stop_when_ready=_get_bool("STOP_WHEN_READY", True),
        interval_s=_get_float("INTERVAL_S", 1.5),
        lamp_warmup_s=_get_float("LAMP_WARMUP_S", 2.0),
        lamp_cooldown_s=_get_float("LAMP_COOLDOWN_S", 1.0),
        save_frames=_get_bool("SAVE_FRAMES", True),
        use_camera_profile=_get_bool("USE_CAMERA_PROFILE", True),
        publish_to=_publish_target(_get("PUBLISH_TO", "auto")),
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


def _publish_target(raw: str) -> str:
    """Laser PUBLISH_TO ur .env. Ett okant varde blir 'auto'.

    'auto' ar standard: MQTT om brokern svarar, annars Home Assistants eget API.
    """
    value = (raw or "auto").strip().lower()
    return value if value in {"auto", "mqtt", "rest", "bada"} else "auto"


def _read_mode(raw: str) -> str:
    """Laser MODE ur .env. Ett okant varde blir 'intervall'."""
    value = (raw or "intervall").strip().lower()
    return value if value in {"natt", "intervall", "manuell"} else "intervall"


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
