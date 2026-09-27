"""Webbgranssnitt och JSON-API for vattenkameran.

Ligger i samma program som laser displayen, sa det behovs ingen extra
programvara i containern och inga nya beroenden - bara Pythons egen http-server
och en enda HTML-fil.

    /                     granssnittet: live-status, senaste vardet och alla
                          installningar (samma .env som kommandoraden laser)
    /api/latest           senaste lasningen som JSON
    /api/history          alla lasningar (underlaget for grafen)
    /api/camera           kamerans installningar (las/lage)
    /api/health           lever tjansten, och nar kor nasta lasning
    /api/config           installningarna (hemliga varden lamnas aldrig ut)
    /api/run              en lasning direkt
    /api/test/camera      prova kameran
    /api/test/ha          prova Home Assistant
    /api/test/mqtt        prova MQTT-brokern
    /api/restart          starta om tjansten (sa att andrade tider slar igenom)
    /api/log              sista raderna ur loggen
    /api/frames/<fil>     en bild fran en korning

Senaste lasningen sparas i latest.json, sa att svaret finns kvar aven om
tjansten startas om.

Kor:
    python status_server.py            # lyssnar pa STATUS_PORT ur .env
    python status_server.py --port 8099
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from config import (
    CAPTURES_DIR,
    INTERVAL_PAUSE_S,
    LATEST_FILE,
    LOG_FILE,
    ROOT,
    STATUS_ALLOW_RESTART,
    STATUS_ALLOW_RUN,
    STATUS_BIND,
    STATUS_LIVE,
    STATUS_PORT,
    VERSION,
)

log = logging.getLogger("granssnitt")

WEB_DIR = ROOT / "web"
SERVICE_NAME = os.getenv("SERVICE_NAME", "vatten-kamera")
MAX_LOG_LINES = 500
PORT = STATUS_PORT

# Delat lage mellan korningen och granssnittet - de lever i samma process.
_STATE: dict[str, Any] = {
    "running": False,
    "started": None,
    "last_finished": None,
    "next_run": None,
    "pid": os.getpid(),
    "boot": time.time(),
}


def set_state(**kwargs: Any) -> None:
    """Uppdaterar laget som granssnittet visar."""
    _STATE.update(kwargs)


def get_state() -> dict[str, Any]:
    return dict(_STATE)


# ---------------------------------------------------------------------------
# Lasning av filer
# ---------------------------------------------------------------------------


def read_latest(path: Path | None = None) -> dict[str, Any]:
    """Senaste lasningen, eller en forklaring om ingen gjorts an."""
    path = path or LATEST_FILE
    if not path.exists():
        return {"ok": False, "version": VERSION, "error": "ingen lasning har gjorts an"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("kunde inte lasa %s: %s", path, exc)
        return {"ok": False, "version": VERSION, "error": f"kunde inte lasa {path.name}: {exc}"}


def tail_log(lines: int = 200, path: Path | None = None) -> list[str]:
    """Sista raderna ur loggfilen."""
    path = path or LOG_FILE
    if not path.exists():
        return [f"ingen loggfil an ({path})"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"kunde inte lasa loggen: {exc}"]
    return text[-max(1, min(lines, MAX_LOG_LINES)) :]


def next_run_at(
    run_at: str,
    pre_start_s: float = 0.0,
    *,
    mode: str = "intervall",
    every_minutes: float = 5.0,
    last_finished: str | None = None,
) -> str | None:
    """Nar nasta lasning borjar, som ISO-tid.

    None betyder att ingen automatisk lasning vantar - i lage 'manuell' startar
    lasningen bara nar nagon trycker pa knappen.
    """
    if mode == "manuell":
        return None

    if mode == "intervall":
        if not last_finished:
            return None
        try:
            sedan = datetime.fromisoformat(last_finished)
        except ValueError:
            return None
        if every_minutes <= 0:
            # "Hela tiden": nasta lasning startar strax efter den forra.
            return (sedan + timedelta(seconds=INTERVAL_PAUSE_S)).isoformat(timespec="seconds")
        target = sedan + timedelta(seconds=max(60.0, every_minutes * 60.0))
        return target.isoformat(timespec="seconds")

    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", (run_at or "").strip())
    if not match:
        return None
    hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    target -= timedelta(seconds=pre_start_s)
    if target <= now:
        target += timedelta(days=1)
    return target.isoformat(timespec="seconds")


def read_history(hours: float = 24.0, limit: int = 2000) -> dict[str, Any]:
    """Lasningarna bakåt i tiden, for grafen i granssnittet.

    Bara det granssnittet behover: tid, varde och adressen till bilden som
    visade vardet - sa att en punkt i grafen gar att klicka pa.
    """
    import history

    punkter: list[dict[str, Any]] = []
    for entry in history.read(hours=hours, limit=limit):
        punkter.append(
            {
                "tid": entry.get("read_at_iso") or entry.get("read_at"),
                "varde": entry.get("numeric"),
                "visas_som": entry.get("display"),
                "ok": bool(entry.get("ok")),
                "konfidens": entry.get("confidence"),
                "roster": entry.get("votes"),
                "bilder": entry.get("frames"),
                "bild_url": frame_url(entry.get("bild")),
                "fel": entry.get("error") or "",
                # Flodet just nu, sa att grafen kan rita det som en egen linje.
                "flode": entry.get("flow_numeric"),
                "flode_text": entry.get("flow"),
            }
        )
    return {"ok": True, "antal": len(punkter), "punkter": punkter}


def frame_url(path: str | None) -> str | None:
    """Gor en filsokvag om till en adress granssnittet kan visa."""
    if not path:
        return None
    try:
        relative = Path(path).resolve().relative_to(CAPTURES_DIR.resolve())
    except (ValueError, OSError):
        return None
    return "/api/frames/" + relative.as_posix()


# ---------------------------------------------------------------------------
# Provar kameran, Home Assistant och MQTT
# ---------------------------------------------------------------------------


def test_camera() -> dict[str, Any]:
    """Tar en bild och matar hur lange det tog."""
    from camera import CameraError, HikvisionCamera
    from config import load_config

    cfg = load_config()
    camera = HikvisionCamera(cfg.camera)
    try:
        started = time.perf_counter()
        frame = camera.snapshot()
        elapsed = (time.perf_counter() - started) * 1000
        target = CAPTURES_DIR / "kameratest.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(frame.jpeg)
        height, width = frame.image.shape[:2]
        return {
            "ok": True,
            "ms": round(elapsed),
            "bild": frame_url(str(target)),
            "upplosning": f"{width}x{height}",
            "kb": round(len(frame.jpeg) / 1024),
            "text": f"kameran svarade pa {elapsed:.0f} ms ({width}x{height})",
        }
    except CameraError as exc:
        return {"ok": False, "text": f"kameran svarade inte: {exc}"}
    except Exception as exc:  # noqa: BLE001 - granssnittet far aldrig krascha
        return {"ok": False, "text": f"fel mot kameran: {exc}"}
    finally:
        camera.close()


def test_ha() -> dict[str, Any]:
    """Provar Home Assistants API och, om den ar satt, lampan."""
    from config import load_config

    cfg = load_config()
    if not cfg.ha.base_url or not cfg.ha.token:
        return {"ok": False, "text": "HA_BASE_URL eller HA_TOKEN saknas"}

    headers = {"Authorization": f"Bearer {cfg.ha.token}", "Content-Type": "application/json"}
    try:
        response = requests.get(f"{cfg.ha.base_url}/api/", headers=headers, timeout=8)
    except requests.RequestException as exc:
        return {"ok": False, "text": f"naddes inte: {exc}"}

    if response.status_code >= 400:
        return {"ok": False, "text": f"HTTP {response.status_code} - kontrollera HA_TOKEN"}

    result: dict[str, Any] = {"ok": True, "text": "Home Assistant svarar"}

    if cfg.ha.light_entity:
        try:
            state = requests.get(
                f"{cfg.ha.base_url}/api/states/{cfg.ha.light_entity}", headers=headers, timeout=8
            )
            if state.status_code == 200:
                lamp = state.json().get("state", "?")
                result["lampa"] = lamp
                result["text"] += f", lampan {cfg.ha.light_entity} ar {lamp}"
            else:
                result["lampa"] = "finns inte"
                result["text"] += f", men {cfg.ha.light_entity} hittades inte"
        except requests.RequestException as exc:
            result["text"] += f", lampan kunde inte lasas: {exc}"

    return result


def test_mqtt() -> dict[str, Any]:
    """Provar MQTT-brokern."""
    from config import load_config
    from mqtt_publisher import MqttPublisher

    cfg = load_config()
    if not cfg.mqtt.enabled or not cfg.mqtt.host:
        return {"ok": False, "text": "ingen MQTT-broker ar konfigurerad"}

    broker = MqttPublisher(cfg.mqtt)
    started = time.perf_counter()
    connected = broker.connect()
    elapsed = (time.perf_counter() - started) * 1000
    broker.disconnect()

    if not connected:
        return {"ok": False, "text": f"brokern {cfg.mqtt.host}:{cfg.mqtt.port} nekade anslutning"}
    return {"ok": True, "text": f"ansluten till {cfg.mqtt.host}:{cfg.mqtt.port} pa {elapsed:.0f} ms"}


def camera_status() -> dict[str, Any]:
    """Kamerans installningar just nu, och om det finns en lasprofil.

    Allt kameralage ligger hos kameran sjalv och i `camera_profile.json` -
    aldrig i koden. Granssnittet visar bara vad kameran svarar.
    """
    from camera_settings import (
        BACKUP_FILE,
        PROFILE_FILE,
        CameraSettings,
        CameraSettingsError,
    )
    from config import load_config

    cfg = load_config()
    if not cfg.camera.ip:
        return {
            "ok": False,
            "text": "kamerans adress saknas - fyll i den under Installningar nedan",
            "har_profil": PROFILE_FILE.exists(),
            "har_backup": BACKUP_FILE.exists(),
        }

    kamera = CameraSettings(cfg.camera)
    try:
        values = kamera.read()
    except CameraSettingsError as exc:
        return {"ok": False, "text": str(exc), "adress": cfg.camera.base_url}

    return {
        "ok": True,
        "adress": cfg.camera.base_url,
        "values": values,
        "profile": kamera.load_profile() or {},
        "har_profil": PROFILE_FILE.exists(),
        "har_backup": BACKUP_FILE.exists(),
        "roi": ",".join(str(part) for part in cfg.calibration_roi) if cfg.calibration_roi else "",
        "laser_med_profil": cfg.run.use_camera_profile,
    }


def camera_action(action: str, values: dict[str, str] | None = None) -> dict[str, Any]:
    """Gor nagot med kameran: byt lage, spara lasprofil, backa upp eller aterstall.

    "lasning" lagger pa den sparade lasprofilen (det ljusare laget siffrorna
    behover), "natt" ger tillbaka kamerans eget nattlage.
    """
    from camera_settings import PROFILE_FILE, CameraSettings, CameraSettingsError
    from config import load_config

    cfg = load_config()
    if not cfg.camera.ip:
        return {"ok": False, "text": "kamerans adress saknas - fyll i den forst"}

    kamera = CameraSettings(cfg.camera)
    try:
        if action == "las_om":
            kamera.read()
            return {"ok": True, "text": "kamerans installningar lasta", "values": kamera.values}

        if action == "lasning":
            profil = kamera.load_profile()
            if not profil:
                return {
                    "ok": False,
                    "text": "ingen lasprofil sparad - stall in laget du vill ha och tryck "
                    "'Spara som lasprofil'",
                }
            values_changed = kamera.apply(profil)
            return {"ok": True, "text": "kameran ar i laslaget", "values": values_changed}

        if action == "natt":
            values_changed = kamera.apply({"ircut": "night", "exposure_type": "auto"})
            return {
                "ok": True,
                "text": "kameran ar tillbaka i sitt eget nattlage",
                "values": values_changed,
            }

        if action == "spara_profil":
            if not kamera.xml:
                kamera.read()
            path = kamera.save_profile()
            return {
                "ok": True,
                "text": f"lasprofilen sparad ({path.name}) - den anvands vid varje lasning",
                "profile": kamera.load_profile() or {},
            }

        if action == "rensa_profil":
            if PROFILE_FILE.exists():
                PROFILE_FILE.unlink()
                return {"ok": True, "text": "lasprofilen borttagen - kameran lamnas som den ar"}
            return {"ok": True, "text": "det fanns ingen lasprofil"}

        if action == "backa_upp":
            kamera.backup()  # skriver aldrig over en befintlig backup
            return {"ok": True, "text": "kamerans utgangslage finns i backupen"}

        if action == "aterstall":
            values_changed = kamera.restore()
            return {"ok": True, "text": "kameran aterstalld fran backupen", "values": values_changed}

        if action == "set":
            if not values:
                return {"ok": False, "text": "inga installningar skickades med"}
            values_changed = kamera.apply(values)
            return {"ok": True, "text": "kamerans installningar andrade", "values": values_changed}

        return {"ok": False, "text": f"okand atgard: {action}"}
    except CameraSettingsError as exc:
        return {"ok": False, "text": str(exc)}


# ---------------------------------------------------------------------------
# Kalibreringen: rutnatet som ser siffrorna
#
# Sifferlasningen hanger pa fyra rutor (cell_boxes i calibration.json) som sager
# var pa bilden varje siffra sitter. Kameran kan rubbas - den sitter i en kallare
# och far en knuff nar saltet fylls pa - och da pekar rutorna fel och inget varde
# publiceras. De har vaggarna later granssnittet visa bilden med rutorna, flytta
# dem med musen och se direkt vad de laser, i stallet for att mata pa en annan
# maskin och kopiera over en fil.
# ---------------------------------------------------------------------------

# Hur manga bilder "Ny bild" och "Mat automatiskt" tar. Fler bilder ger en
# stadigare tidsstack (alla fyra siffrorna syns da pa en gang) men tar langre
# tid: 8 bilder med 1.2 s mellanrum ar ett tiotal sekunder.
KALIBRERINGS_BILDER = 8
KALIBRERINGS_INTERVALL_S = 1.2

# En ruta mindre an sa kan inte innehalla en siffra.
MIN_CELL_PX = 20

# Hur mycket luft rutnatet far ha runt siffrorna nar ROI:n behover vidgas.
ROI_MARGINAL = 48

# Luften runt ROI:n i vyn, sa att en ruta som hamnat utanfor anda syns.
VY_MARGINAL = 150

# Den senaste bilden. "Spara" visar da vad de nya rutorna laser i exakt samma
# bild som man drog i, och sidan behover inte ta nya bilder varje gang.
_BILD: dict[str, Any] = {"stack": None, "frames": [], "jpeg": None, "tagen": None}
_BILD_LAS = threading.Lock()


def _kalibreringsfil() -> Path:
    """Var kalibreringen ligger - las vid anropet, sa att ett test kan byta fil."""
    from config import load_config

    return load_config().calibration_file


def _las_kalibrering() -> tuple[Any, str]:
    """Kalibreringen ur filen, och ett felmeddelande om den inte gick att lasa.

    Saknas filen far granssnittet anda visa en bild - annars gar den forsta
    kalibreringen inte att gora darifran.
    """
    from config import load_config
    from display_reader import Calibration, ReaderError

    cfg = load_config()
    try:
        return Calibration.load(_kalibreringsfil()), ""
    except (ReaderError, ValueError, TypeError, KeyError) as exc:
        roi = tuple(cfg.calibration_roi) if cfg.calibration_roi else (0, 0, 0, 0)
        return Calibration(roi=roi, digit_count=cfg.reader.digit_count or 4), str(exc)


def vanta_pa_kameran(timeout_s: float = 30.0) -> bool:
    """Ber en pagaende lasning att sluta, och vantar tills kameran ar ledig.

    Kameran (en 2014-modell) svarar bara en i taget: tva samtidiga pollningar ger
    'Connection aborted', och da tar bada dubbelt sa lang tid i stallet. En
    korning som letar forgaves kan halla pa i 30 minuter, sa den maste slappa.
    """
    from pipeline import begar_avbrott

    if not get_state().get("running"):
        return True
    begar_avbrott()
    slut = time.time() + timeout_s
    while time.time() < slut:
        if not get_state().get("running"):
            log.info("kalibreringen tar over kameran - lasningen avbruten")
            return True
        time.sleep(0.5)
    log.warning("en lasning paga fortfarande - kalibrerar anda, det tar langre tid")
    return False


def _ta_bilder(
    antal: int = KALIBRERINGS_BILDER, intervall: float = KALIBRERINGS_INTERVALL_S
) -> list[Any]:
    """Tar farska bilder fran kameran."""
    from camera import CameraError, HikvisionCamera
    from config import load_config

    cfg = load_config()
    if not cfg.camera.ip:
        raise ValueError("kamerans adress saknas - fyll i den under Installningar")

    vanta_pa_kameran()
    kamera = HikvisionCamera(cfg.camera)
    bilder: list[Any] = []
    try:
        for nummer in range(max(1, antal)):
            start = time.perf_counter()
            try:
                bilder.append(kamera.snapshot().image)
            except CameraError as exc:
                log.warning("kameran svarade inte pa bild %d: %s", nummer + 1, exc)
            kvar = intervall - (time.perf_counter() - start)
            if nummer < antal - 1 and kvar > 0:
                time.sleep(kvar)
    finally:
        kamera.close()

    if not bilder:
        raise ValueError("kameran svarade inte - se loggen")
    return bilder


def _tidsstack(frames: list[Any]) -> Any:
    """En bild dar ALLA sidor syns: den ljusaste pixeln av varje.

    Displayen visar en sida i taget, och pa vardesidan ar forsta positionen
    slackt. Genom att lagga bilderna ovanpa varandra syns en siffra i varje
    position, och da gar det att se om en ruta sitter ratt.
    """
    import numpy as np

    return np.max(np.stack(frames), axis=0)


def vy_runt(
    roi: tuple[int, int, int, int], storlek: tuple[int, int], marginal: int = VY_MARGINAL
) -> tuple[int, int, int, int]:
    """Utsnittet som visas i granssnittet: ROI:n med luft runt omkring."""
    bredd, hojd = storlek
    x1 = max(0, int(roi[0]) - marginal)
    y1 = max(0, int(roi[1]) - marginal)
    x2 = min(bredd, int(roi[2]) + marginal)
    y2 = min(hojd, int(roi[3]) + marginal)
    return (x1, y1, x2, y2)


def las_med_rutorna(frames: list[Any], cal: Any) -> dict[str, Any]:
    """Vad rutnatet laser i bilderna, och vilken siffra som ar svagast."""
    from config import load_config
    from display_reader import read_image

    cfg = load_config()
    vald: Any = None
    for bild in frames:
        try:
            lasning = read_image(bild, cal, cfg.reader)
        except Exception as exc:  # noqa: BLE001 - en trasig bild far inte stoppa vyn
            log.warning("kunde inte tolka en bild: %s", exc)
            continue
        # En vardesida - vardet star i position 2-4 - ar den vi vill visa.
        vardesida = bool(lasning.digits) and lasning.digits[0].blank
        if lasning.ok and lasning.confidence > 0 and vardesida:
            vald = lasning
            break
        if vald is None or lasning.confidence > vald.confidence:
            vald = lasning

    if vald is None:
        return {
            "ok": False,
            "varde": "",
            "display": "",
            "konfidens": 0.0,
            "siffror": [],
            "text": "kunde inte tolka bilden alls",
        }

    siffror = [
        {"position": index, "tecken": digit.char, "konfidens": round(digit.confidence, 2)}
        for index, digit in enumerate(vald.digits, start=1)
    ]
    tande = [item for item in siffror if item["tecken"] != " "]
    svagast = min(tande, key=lambda item: item["konfidens"], default=None)
    display = ""
    if vald.numeric is not None:
        display = f"{vald.numeric:.{vald.decimals}f}"

    # En vardesida har forsta positionen slackt; pa en tidssida (klockan, 02:00)
    # lyser alla fyra. Bada sager lika mycket om rutorna sitter ratt, men bara
    # vardesidan ger ett varde - och det ska sta, inte "rutorna pekar fel".
    vardesida = bool(vald.digits) and vald.digits[0].blank
    if vardesida:
        sida = "varde"
    elif vald.digits and len(tande) == len(vald.digits):
        sida = "tid"
    else:
        sida = "okant"

    if not tande:
        text = "ingen siffra hittades - sitter rutorna innanfor displayen?"
        ok = False
    elif vardesida and vald.confidence > 0:
        text = f"rutorna laser {display} (konfidens {vald.confidence:.2f})"
        ok = True
    elif sida == "tid" and svagast["konfidens"] >= 0.5:
        # Alla fyra siffrorna lases sakert, men sidan ar en tidssida - det finns
        # inget varde att visa forran en vardesida kommer.
        text = (
            f"siffrorna lases sakert - displayen visar en tidssida ({display})."
            " Ta 'Ny bild' for att se en vardesida."
        )
        ok = True
    else:
        text = (
            f"position {svagast['position']} laser {svagast['tecken']!r} med konfidens"
            f" {svagast['konfidens']:.2f} - flytta den rutan"
        )
        ok = svagast["konfidens"] >= 0.5

    return {
        "ok": ok,
        "sida": sida,
        "varde": vald.value or "",
        "display": display,
        "konfidens": round(vald.confidence, 2),
        "minsta_konfidens": svagast["konfidens"] if svagast else 0.0,
        "siffror": siffror,
        "svagast": svagast,
        "text": text,
    }


def _koda_bild(stack: Any) -> bytes | None:
    """Gor om tidsstacken till en JPEG som sidan kan visa."""
    try:
        import cv2

        ok, kodad = cv2.imencode(".jpg", stack, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        return kodad.tobytes() if ok else None
    except Exception as exc:  # noqa: BLE001
        log.warning("kunde inte koda kalibreringsbilden: %s", exc)
        return None


def _spara_bilden(frames: list[Any]) -> None:
    """Kommer ihag de sista bilderna, sa att 'Spara' kan visa dem igen."""
    with _BILD_LAS:
        stack = _tidsstack(frames)
        _BILD["frames"] = frames
        _BILD["stack"] = stack
        _BILD["jpeg"] = _koda_bild(stack)
        _BILD["tagen"] = time.time()


def kalibreringsvy(*, ta_nya: bool = False) -> dict[str, Any]:
    """Kalibreringen, en bild av displayen och vad rutorna laser i den."""
    from config import load_config

    cfg = load_config()
    if ta_nya or not _BILD["frames"]:
        _spara_bilden(_ta_bilder())

    cal, fel = _las_kalibrering()
    hojd, bredd = _BILD["stack"].shape[:2]
    roi = tuple(cal.roi) if cal.valid else (0, 0, bredd, hojd)
    tagen = float(_BILD["tagen"] or time.time())

    return {
        "ok": True,
        "version": VERSION,
        "roi": list(roi),
        "vy": list(vy_runt(roi, (bredd, hojd))),
        "storlek": [bredd, hojd],
        "cell_boxes": [list(box) for box in cal.cell_boxes],
        "digit_count": int(cal.digit_count or cfg.reader.digit_count or 4),
        "kalibrering_saknas": bool(fel),
        "fel": fel,
        "bild_url": f"/api/calibration/bild?t={int(tagen)}" if _BILD["jpeg"] else "",
        "lasning": las_med_rutorna(_BILD["frames"], cal),
        "text": f"bild tagen {datetime.fromtimestamp(tagen).strftime('%H:%M:%S')}"
        f" ({len(_BILD['frames'])} bilder)",
    }


def _giltiga_rutor(ra: Any, antal: int) -> list[tuple[int, int, int, int]]:
    """Kontrollerar rutorna - en ruta som ar fel ger en tyst fel lasning."""
    if not isinstance(ra, list) or len(ra) != antal:
        raise ValueError(f"det ska vara {antal} rutor (en per siffra)")
    rutor: list[tuple[int, int, int, int]] = []
    for index, ruta in enumerate(ra, start=1):
        if not isinstance(ruta, (list, tuple)) or len(ruta) != 4:
            raise ValueError(f"ruta {index}: x1,y1,x2,y2 kravs")
        try:
            x1, y1, x2, y2 = (int(round(float(varde))) for varde in ruta)
        except (TypeError, ValueError):
            raise ValueError(f"ruta {index}: bara siffror") from None
        if x2 - x1 < MIN_CELL_PX or y2 - y1 < MIN_CELL_PX:
            raise ValueError(f"ruta {index} ar for liten (minst {MIN_CELL_PX} px)")
        if min(x1, y1) < 0 or max(x2, y2) > 4096:
            raise ValueError(f"ruta {index} ligger utanfor bilden")
        rutor.append((x1, y1, x2, y2))
    return rutor


def spara_kalibrering(payload: dict[str, Any], *, notis: str = "") -> dict[str, Any]:
    """Sparar rutnatet och visar vad det laser i samma bild som man drog i."""
    from config import load_config
    from display_reader import Calibration
    from settings_store import apply_changes

    cfg = load_config()
    cal, _fel = _las_kalibrering()
    antal = int(cal.digit_count or cfg.reader.digit_count or 4)
    rutor = _giltiga_rutor(payload.get("cell_boxes"), antal)

    # ROI:n maste rymma rutorna - annars klipps siffran bort innan rutorna ens
    # far se den. Den vidgas bara nar nagon ruta ligger utanfor, annars skulle
    # den vaxa en bit varje gang man sparar. Den krymps aldrig.
    gammal = tuple(cal.roi) if cal.valid else (0, 0, 0, 0)
    utanfor = (
        not cal.valid
        or min(box[0] for box in rutor) < gammal[0]
        or min(box[1] for box in rutor) < gammal[1]
        or max(box[2] for box in rutor) > gammal[2]
        or max(box[3] for box in rutor) > gammal[3]
    )
    if utanfor:
        ny_roi = (
            max(0, min(gammal[0], min(box[0] for box in rutor) - ROI_MARGINAL)),
            max(0, min(gammal[1], min(box[1] for box in rutor) - ROI_MARGINAL)),
            max(gammal[2], max(box[2] for box in rutor) + ROI_MARGINAL),
            max(gammal[3], max(box[3] for box in rutor) + ROI_MARGINAL),
        )
    else:
        ny_roi = gammal

    ny = Calibration(
        roi=ny_roi,
        digit_count=antal,
        cell_boxes=list(rutor),
        notes=notis
        or f"rutnat flyttat i granssnittet {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    )
    ny.save(_kalibreringsfil())

    roi_text = ""
    if ny_roi != gammal:
        # Verktygen (tools/fit_cells.py, main.py calibrate) mater mot ROI:n i
        # .env - utan den har raden skulle de mata mot ett gammalt utsnitt.
        changed, problems = apply_changes(
            {"CALIBRATION_ROI": ",".join(str(varde) for varde in ny_roi)}
        )
        if changed and not problems:
            roi_text = f" - utsnittet vidgat till {','.join(str(v) for v in ny_roi)}"
        elif problems:
            roi_text = " - OBS: utsnittet behovde vidgas men gick inte att skriva i .env"

    lasning = las_med_rutorna(_BILD["frames"], ny) if _BILD["frames"] else {}
    return {
        "ok": True,
        "version": VERSION,
        "roi": list(ny_roi),
        "cell_boxes": [list(box) for box in rutor],
        "lasning": lasning,
        "text": f"rutnatet sparat{roi_text}",
    }


def mat_kalibrering() -> dict[str, Any]:
    """Mater fram rutnatet i farska bilder - samma vag som 'main.py calibrate'."""
    from config import load_config
    from display_reader import ReaderError, measure_cells

    cfg = load_config()
    cal, _fel = _las_kalibrering()
    roi = tuple(cal.roi) if cal.valid else tuple(cfg.calibration_roi or (0, 0, 0, 0))
    if not (roi[2] > roi[0] and roi[3] > roi[1]):
        raise ValueError("CALIBRATION_ROI saknas - fyll i utsnittet under Installningar forst")

    frames = _ta_bilder()
    prior = [tuple(box) for box in cal.cell_boxes] or None
    try:
        rutor, rapport = measure_cells(
            frames, roi, cfg.reader, cfg.reader.digit_count, prior=prior
        )
    except ReaderError as exc:
        raise ValueError(f"kunde inte mata rutnatet: {exc}") from None

    _spara_bilden(frames)
    svar = spara_kalibrering(
        {"cell_boxes": [list(box) for box in rutor]},
        notis=f"rutnat matt fram i granssnittet {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    )
    svar["rapport"] = list(rapport)
    svar["text"] = "rutnatet matt fram och sparat"
    return svar


def start_run() -> dict[str, Any]:
    """Startar en lasning i bakgrunden - den tar en dryg minut."""
    if get_state().get("running"):
        return {"ok": False, "text": "en lasning paga redan"}

    def worker() -> None:
        from config import load_config
        from pipeline import NightlyRunner

        set_state(running=True, started=datetime.now().isoformat(timespec="seconds"))
        try:
            cfg = load_config()
            runner = NightlyRunner(cfg, use_lamp=True)
            summary = runner.run_once(save=True)
            log.info("lasning klar: %s", summary.value)
        except Exception:  # noqa: BLE001
            log.exception("lasningen misslyckades")
        finally:
            set_state(running=False, last_finished=datetime.now().isoformat(timespec="seconds"))

    threading.Thread(target=worker, name="lasning", daemon=True).start()
    return {"ok": True, "text": "lasningen startad - den tar en dryg minut"}


def restart_service() -> dict[str, Any]:
    """Startar om tjansten, sa att andrade tider och adresser slar igenom."""
    if os.name == "nt":
        return {"ok": False, "text": "starta om tjansten sjalv har (Windows)"}
    try:
        result = subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "text": f"kunde inte starta om: {exc}"}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {"ok": False, "text": f"systemctl svarade {result.returncode}: {detail}"}
    return {"ok": True, "text": f"{SERVICE_NAME} startar om"}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _handler_factory(*, allow_read: bool, allow_restart: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"vatten_kamera/{VERSION}"
        protocol_version = "HTTP/1.1"

        # --- GET ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - http.server kraver namnet
            parsed = urlparse(self.path)
            path = parsed.path

            if path in {"/", "/index.html"}:
                self._send_file(WEB_DIR / "index.html")
                return
            if path in {"/api", "/api/latest"}:
                self._send_json(_latest_with_url())
                return
            if path == "/api/health":
                self._send_json(self._health())
                return
            if path == "/api/config":
                from settings_store import current

                self._send_json({"ok": True, "version": VERSION, "fields": current()})
                return
            if path == "/api/log":
                match = re.search(r"lines=(\d+)", parsed.query)
                self._send_json({"ok": True, "lines": tail_log(int(match.group(1)) if match else 200)})
                return
            if path == "/api/history":
                timmar = re.search(r"hours=([\d.]+)", parsed.query)
                antal = re.search(r"limit=(\d+)", parsed.query)
                self._send_json(
                    read_history(
                        hours=float(timmar.group(1)) if timmar else 24.0,
                        limit=int(antal.group(1)) if antal else 2000,
                    )
                )
                return
            if path.startswith("/api/frames/"):
                self._send_frame(unquote(path[len("/api/frames/") :]))
                return
            if path in {"/api/test/camera", "/api/test/ha", "/api/test/mqtt"}:
                self._send_json(self._test(path.rsplit("/", 1)[-1]))
                return
            if path == "/api/camera":
                try:
                    self._send_json(camera_status())
                except Exception as exc:  # noqa: BLE001
                    log.exception("kameravagen kraschade")
                    self._send_json({"ok": False, "text": f"kunde inte lasa kameran: {exc}"})
                return
            if path == "/api/calibration":
                # Tar bilder fran kameran forsta gangen (nagra sekunder), sedan
                # serveras den bilden - sa att sidan kan ritas om snabbt.
                try:
                    self._send_json(kalibreringsvy())
                except Exception as exc:  # noqa: BLE001
                    log.exception("kalibreringsvyn kraschade")
                    self._send_json({"ok": False, "text": f"kunde inte hamta nagon bild: {exc}"})
                return
            if path == "/api/calibration/bild":
                if not _BILD["jpeg"]:
                    self._send_json({"ok": False, "error": "ingen bild tagen an"}, status=404)
                    return
                self._send_bytes(_BILD["jpeg"], "image/jpeg")
                return

            self._send_json({"ok": False, "error": f"okand vag {path}"}, status=404)

        # --- POST --------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            payload = self._read_json()

            if path == "/api/config":
                from settings_store import apply_changes, restart_required

                changed, problems = apply_changes(payload)
                if problems:
                    self._send_json({"ok": False, "fel": problems}, status=400)
                    return

                # Nastan allt slar igenom vid nasta lasning: tjansten laser om
                # .env mellan korningarna. Bara granssnittets egen port och
                # av/på-knapparna kraver en omstart.
                omstart = restart_required(changed)
                if not changed:
                    text = "inga andringar"
                elif omstart:
                    text = (
                        f"{len(changed)} installning(ar) sparade - "
                        f"{', '.join(omstart)} kraver att tjansten startas om"
                    )
                else:
                    text = (
                        f"{len(changed)} installning(ar) sparade - "
                        "de galler fran nasta lasning"
                    )
                self._send_json(
                    {
                        "ok": True,
                        "text": text,
                        "andrade": sorted(changed),
                        "omstart_kravs": omstart,
                    }
                )
                return

            if path == "/api/run":
                if not (allow_read and STATUS_ALLOW_RUN):
                    self._send_json(
                        {"ok": False, "text": "lasning direkt ar avstangd (STATUS_ALLOW_RUN)"},
                        status=403,
                    )
                    return
                self._send_json(start_run())
                return

            if path == "/api/restart":
                if not (allow_restart and STATUS_ALLOW_RESTART):
                    self._send_json(
                        {"ok": False, "text": "omstart ar avstangd (STATUS_ALLOW_RESTART)"},
                        status=403,
                    )
                    return
                self._send_json(restart_service())
                return

            if path == "/api/camera":
                # Kamerans lage andras pa samma villkor som en lasning direkt:
                # den som far starta en lasning far ocksa rora kameran.
                if not (allow_read and STATUS_ALLOW_RUN):
                    self._send_json(
                        {"ok": False, "text": "andringar av kameran ar avstangda (STATUS_ALLOW_RUN)"},
                        status=403,
                    )
                    return
                self._send_json(camera_action(str(payload.get("action") or ""), payload.get("values")))
                return

            if path in {"/api/calibration", "/api/calibration/ny", "/api/calibration/mat"}:
                # Kalibreringen ror kameran och skriver calibration.json, sa den
                # gar pa samma villkor som en lasning direkt.
                if not (allow_read and STATUS_ALLOW_RUN):
                    self._send_json(
                        {"ok": False, "text": "kalibreringen ar last (STATUS_ALLOW_RUN)"},
                        status=403,
                    )
                    return
                try:
                    if path.endswith("/ny"):
                        self._send_json(kalibreringsvy(ta_nya=True))
                    elif path.endswith("/mat"):
                        self._send_json(mat_kalibrering())
                    else:
                        self._send_json(spara_kalibrering(payload))
                except ValueError as exc:
                    self._send_json({"ok": False, "text": str(exc)}, status=400)
                except Exception as exc:  # noqa: BLE001
                    log.exception("kalibreringen kraschade")
                    self._send_json({"ok": False, "text": f"kalibreringen kraschade: {exc}"})
                return

            self._send_json({"ok": False, "error": f"okand vag {path}"}, status=404)

        # --- Hjalpare ----------------------------------------------------
        def _health(self) -> dict[str, Any]:
            from config import load_config

            import history

            try:
                cfg = load_config()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "version": VERSION, "error": str(exc)}

            state = get_state()
            latest = read_latest()
            lackage = history.flode_larm(
                troskel=cfg.run.flow_warn,
                minuter=cfg.run.flow_warn_minutes,
            )
            age = None
            if latest.get("read_at"):
                try:
                    age = round(
                        (datetime.now() - datetime.fromisoformat(latest["read_at"])).total_seconds()
                    )
                except ValueError:
                    age = None

            return {
                "ok": True,
                "version": VERSION,
                "host": socket.gethostname(),
                "pid": state.get("pid"),
                "uppe_s": round(time.time() - float(state.get("boot") or time.time())),
                "kor": bool(state.get("running")),
                # Kor den automatiska lasloopen? Granssnittet kan startas ensamt
                # (main.py status), och da laser ingenting av sig sjalv.
                "loop": bool(state.get("loop")),
                "startad": state.get("started"),
                "senast_klar": state.get("last_finished"),
                "nasta_korning": next_run_at(
                    cfg.run.run_at,
                    cfg.run.pre_start_s,
                    mode=cfg.run.mode,
                    every_minutes=cfg.run.every_minutes,
                    last_finished=state.get("last_finished"),
                ),
                "lage": cfg.run.mode,
                "var_minuter": cfg.run.every_minutes,
                "run_at": cfg.run.run_at,
                # Finns kalibreringen? Utan den kan tjansten inte lasa, och da
                # visar granssnittet en forklaring i stallet for tystnad.
                "kalibrering": cfg.calibration_file.exists(),
                # Flodet just nu, och om det legat kvar sa lange att det ser ut
                # som ett lackage (eller en oppen ventil).
                "flode": latest.get("flow_numeric"),
                "flode_enhet": latest.get("flow_unit") or cfg.run.flow_unit,
                "lackage": bool(lackage.get("larm")),
                "lackage_text": lackage.get("text", ""),
                "lackage_troskel": cfg.run.flow_warn,
                "lackage_minuter": cfg.run.flow_warn_minutes,
                "enhet": cfg.mqtt.unit,
                "aldsta_lasning_s": age,
                "adress": f"http://{socket.gethostname()}:{PORT}",
                "tjanst": SERVICE_NAME,
                # Vad granssnittet far gora - gar att stanga av i .env.
                "live": STATUS_LIVE,
                "far_lasa": allow_read and STATUS_ALLOW_RUN,
                "far_starta_om": allow_restart and STATUS_ALLOW_RESTART,
            }

        def _test(self, what: str) -> dict[str, Any]:
            try:
                if what == "camera":
                    return test_camera()
                if what == "ha":
                    return test_ha()
                return test_mqtt()
            except Exception as exc:  # noqa: BLE001
                log.exception("testet %s kraschade", what)
                return {"ok": False, "text": f"testet kraschade: {exc}"}

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if length <= 0 or length > 1_000_000:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8")) or {}
            except (ValueError, UnicodeDecodeError):
                return {}

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status=status)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self._send_json({"ok": False, "error": "filen finns inte"}, status=404)
                return
            self._send_bytes(
                path.read_bytes(),
                _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            )

        def _send_frame(self, relative: str) -> None:
            """En bild fran en korning - bara innanfor bildkatalogen."""
            try:
                target = (CAPTURES_DIR / relative).resolve()
                target.relative_to(CAPTURES_DIR.resolve())
            except (ValueError, OSError):
                self._send_json({"ok": False, "error": "utanfor bildkatalogen"}, status=403)
                return
            self._send_file(target)

        def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def _latest_with_url() -> dict[str, Any]:
    latest = read_latest()
    latest["bild_url"] = frame_url(latest.get("bild"))
    return latest


class StatusServer:
    """Granssnittet, i en egen trad sa att nattkorningen skots som vanligt."""

    def __init__(
        self,
        *,
        port: int = STATUS_PORT,
        bind: str = STATUS_BIND,
        allow_read: bool = True,
        allow_restart: bool = True,
    ) -> None:
        self.port = port
        self.bind = bind
        self.allow_read = allow_read
        self.allow_restart = allow_restart
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self.port <= 0:
            log.info("STATUS_PORT ar 0 - inget granssnitt startas")
            return False
        if self._httpd is not None:
            return True

        global PORT
        PORT = self.port
        handler = _handler_factory(allow_read=self.allow_read, allow_restart=self.allow_restart)
        try:
            self._httpd = ThreadingHTTPServer((self.bind, self.port), handler)
        except OSError as exc:
            log.error("kunde inte lyssna pa %s:%s (%s)", self.bind, self.port, exc)
            return False

        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="granssnitt", daemon=True
        )
        self._thread.start()
        log.info("granssnittet lyssnar pa http://%s:%s/", self.bind, self.port)
        return True

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Webbgranssnitt for vattenkameran")
    parser.add_argument("--port", type=int, default=STATUS_PORT or 8099)
    parser.add_argument("--bind", default=STATUS_BIND)
    parser.add_argument("--utan-lasning", action="store_true", help="tillat inte lasning direkt")
    parser.add_argument("--utan-omstart", action="store_true", help="tillat inte omstart av tjansten")
    args = parser.parse_args()

    from applog import setup_logging

    setup_logging(False)

    server = StatusServer(
        port=args.port,
        bind=args.bind,
        allow_read=not args.utan_lasning,
        allow_restart=not args.utan_omstart,
    )
    if not server.start():
        return 1
    print(f"oppna http://{args.bind}:{args.port}/  (Ctrl+C for att avsluta)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\navslutar")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
