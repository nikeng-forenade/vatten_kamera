"""Webbgranssnitt och JSON-API for vattenkameran.

Ligger i samma program som laser displayen, sa det behovs ingen extra
programvara i containern och inga nya beroenden - bara Pythons egen http-server
och en enda HTML-fil.

    /                     granssnittet: live-status, senaste vardet och alla
                          installningar (samma .env som kommandoraden laser)
    /api/latest           senaste lasningen som JSON
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
    mode: str = "natt",
    every_minutes: float = 10.0,
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
            if path.startswith("/api/frames/"):
                self._send_frame(unquote(path[len("/api/frames/") :]))
                return
            if path in {"/api/test/camera", "/api/test/ha", "/api/test/mqtt"}:
                self._send_json(self._test(path.rsplit("/", 1)[-1]))
                return

            self._send_json({"ok": False, "error": f"okand vag {path}"}, status=404)

        # --- POST --------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            payload = self._read_json()

            if path == "/api/config":
                from settings_store import apply_changes

                changed, problems = apply_changes(payload)
                if problems:
                    self._send_json({"ok": False, "fel": problems}, status=400)
                    return
                if changed:
                    text = f"{len(changed)} installning(ar) sparade - starta om tjansten"
                else:
                    text = "inga andringar"
                self._send_json({"ok": True, "text": text, "andrade": sorted(changed)})
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

            self._send_json({"ok": False, "error": f"okand vag {path}"}, status=404)

        # --- Hjalpare ----------------------------------------------------
        def _health(self) -> dict[str, Any]:
            from config import load_config

            try:
                cfg = load_config()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "version": VERSION, "error": str(exc)}

            state = get_state()
            latest = read_latest()
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
