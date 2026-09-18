"""Kommandoradsverktyg for vattenkameran.

Exempel:
    python main.py version
    python main.py probe                     # testa kameran
    python main.py calibrate --suggest       # hitta displayen och kalibrera
    python main.py calibrate --roi 430,1185,590,1240 --digits 4
    python main.py read --seconds 15         # las nu och visa resultatet
    python main.py lamp on                   # testa lampan
    python main.py mqtt-test                 # publicera ett provvarde
    python main.py run --seconds 20          # en komplett korning direkt
    python main.py daemon                    # vanta in 02:00 och kor varje natt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

import cv2
import numpy as np

from camera import CameraError, HikvisionCamera
from config import ROOT, VERSION, Config, load_config
from display_reader import (
    Calibration,
    find_band,
    fit_grid,
    preprocess,
    read_image,
)
from ha_client import HaError, HomeAssistant

log = logging.getLogger("main")

DEBUG_DIR = ROOT / "captures"


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# Kommandon
# ---------------------------------------------------------------------------


def cmd_version(cfg: Config, _args: argparse.Namespace) -> int:
    print(f"vatten_kamera {VERSION}")
    print(f"kamera      : {cfg.camera.ip} (kanal {cfg.camera.channel})")
    print(f"lampa       : {cfg.ha.light_entity or '(inte konfigurerad)'}")
    print(f"mqtt        : {'ja' if cfg.mqtt.enabled else 'nej'} {cfg.mqtt.host or ''}")
    print(f"kor kl      : {cfg.run.run_at}  fonster {cfg.run.window_s:.0f} s")
    print(f"kalibrering : {'finns' if cfg.calibration_file.exists() else 'saknas'}")
    return 0


def cmd_probe(cfg: Config, _args: argparse.Namespace) -> int:
    camera = HikvisionCamera(cfg.camera)
    try:
        info = camera.device_info()
        model = _xml_value(info, "model") or "?"
        firmware = _xml_value(info, "firmwareVersion") or "?"
        print(f"kamera svarar: {model}  firmware {firmware}")

        frame = camera.snapshot()
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / f"probe_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(frame.jpeg)
        print(f"snapshot: {frame.width}x{frame.height} px, {len(frame.jpeg)//1024} kB -> {path}")
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()
    return 0


def _xml_value(xml: str, tag: str) -> str | None:
    import re

    match = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
    return match.group(1) if match else None


def cmd_calibrate(cfg: Config, args: argparse.Namespace) -> int:
    """Hittar displayen i en bild och sparar ett kalibrerat rutnat."""
    camera = HikvisionCamera(cfg.camera)
    try:
        frame = camera.snapshot()
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    image = frame.image
    height, width = image.shape[:2]
    print(f"bild: {width}x{height} px")

    if args.roi:
        roi = tuple(int(v) for v in args.roi.split(","))
        if len(roi) != 4:
            print("FEL: --roi ska vara x1,y1,x2,y2")
            return 2
    elif cfg.calibration_roi:
        roi = cfg.calibration_roi
        print(f"ROI fran .env: {roi[0]},{roi[1]},{roi[2]},{roi[3]}")
    else:
        # Foresla ett ROI utifran var siffrorna sitter i hela bilden.
        roi = _suggest_roi(image, cfg)
        print(f"foreslaget ROI: {roi[0]},{roi[1]},{roi[2]},{roi[3]}")

    digits = args.digits or cfg.reader.digit_count

    # Rita en hjalpbild sa man ser att ROI:t sitter ratt.
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    marked = image.copy()
    cv2.rectangle(marked, (roi[0], roi[1]), (roi[2], roi[3]), (0, 0, 255), 4)
    marked_path = DEBUG_DIR / f"calibrate_{stamp}_roi.png"
    cv2.imwrite(str(marked_path), marked)

    normalized, binary = preprocess(image, roi, cfg.reader)
    band = find_band(binary)
    boxes, grid_score = fit_grid(normalized, binary, band, digits)

    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    absolute = [
        (
            int(round(roi[0] + bx1 / scale)),
            int(round(roi[1] + by1 / scale)),
            int(round(roi[0] + bx2 / scale)),
            int(round(roi[1] + by2 / scale)),
        )
        for bx1, by1, bx2, by2 in boxes
    ]

    calibration = Calibration(roi=roi, digit_count=digits, cell_boxes=absolute)
    reading = read_image(image, calibration, cfg.reader, timestamp=frame.timestamp)

    print(f"\nsifferband : {band}")
    print(f"rutnatspoang: {grid_score:.3f}")
    print(f"last varde : {reading.value!r}   konfidens {reading.confidence:.3f}\n")

    for index, (box, digit) in enumerate(zip(absolute, reading.digits), start=1):
        values = " ".join(f"{k}={v:.2f}" for k, v in sorted(digit.segment_values.items()))
        print(f"  siffra {index}: {box} -> {digit.char!r} konfidens {digit.confidence:.2f}")
        print(f"              {values}")

    # Rita ut det valda rutnatet.
    for box in absolute:
        cv2.rectangle(marked, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 2)
    cv2.imwrite(str(marked_path), marked)

    if args.save:
        cfg.calibration_file.write_text(
            json.dumps(
                {
                    "roi": list(roi),
                    "digit_count": digits,
                    "cell_boxes": [list(b) for b in absolute],
                    "notes": f"kalibrerad {datetime.now():%Y-%m-%d %H:%M}",
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nkalibrering sparad -> {cfg.calibration_file}")
    else:
        print("\n(torrkorning - inget sparat. Kor med --save for att spara)")

    print(f"kontrollbild -> {marked_path}")
    return 0


def _suggest_roi(image: np.ndarray, cfg: Config) -> tuple[int, int, int, int]:
    """Foreslar ett ROI runt siffrorna pa displayen.

    Siffrorna lyser starkast i bilden och ligger tat tillsammans, sa vi letar
    upp den klunga av sma ljusa flackar som har flest grannar. Det fungerar
    aven nar kameran ser hela pumphuset och siffrorna bara ar en liten del av
    bilden.
    """
    height, width = image.shape[:2]
    full = (0, 0, width, height)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)

    max_area = 0.02 * width * height
    candidates: list[tuple[float, float, float, float]] = []
    for label in range(1, count):
        x, y, box_w, box_h, area = stats[label]
        if area < 8 or area > max_area:
            continue
        # En flack som ror vid kanten ar oftast bakgrund, inte en siffra.
        if x <= 1 or y <= 1 or x + box_w >= width - 1 or y + box_h >= height - 1:
            continue
        candidates.append((float(x), float(y), float(box_w), float(box_h)))

    if not candidates:
        return full

    best_group: list[tuple[float, float, float, float]] = []
    for x, y, box_w, box_h in candidates:
        center_x = x + box_w / 2.0
        center_y = y + box_h / 2.0
        radius = max(3.0 * box_h, 26.0)
        group = [
            c
            for c in candidates
            if abs((c[0] + c[2] / 2.0) - center_x) <= radius
            and abs((c[1] + c[3] / 2.0) - center_y) <= radius
        ]
        if len(group) > len(best_group):
            best_group = group

    if not best_group:
        return full

    bx1 = min(c[0] for c in best_group)
    by1 = min(c[1] for c in best_group)
    bx2 = max(c[0] + c[2] for c in best_group)
    by2 = max(c[1] + c[3] for c in best_group)

    # Rikligt med marginal, sarskilt pa hojden: bandet av tta pixlar tacker
    # bara mitten av siffrorna, medan vi vill ha hela cellen.
    pad_x = max(12, int(0.35 * (bx2 - bx1)))
    pad_y = max(10, int(0.80 * (by2 - by1)))

    return (
        max(0, int(bx1) - pad_x),
        max(0, int(by1) - pad_y),
        min(width, int(bx2) + pad_x),
        min(height, int(by2) + pad_y),
    )


def cmd_read(cfg: Config, args: argparse.Namespace) -> int:
    """Laser displayen nu och skriver ut resultatet."""
    from pipeline import NightlyRunner

    runner = NightlyRunner(cfg, use_lamp=args.lampa)
    try:
        summary = runner.run_once(duration_s=args.seconds, save=args.spara)
    except FileNotFoundError:
        print("FEL: ingen kalibrering finns. Kor 'python main.py calibrate --save' forst.")
        return 1

    print()
    print(f"bilder lasta  : {summary.frames_taken}")
    print(f"lasbara bilder: {summary.frames_readable}")
    if summary.value:
        print(f"VARDET        : {summary.numeric:.0f}  (text {summary.value!r})")
        print(f"roster        : {summary.votes}/{summary.frames_taken}  konfidens {summary.confidence:.2f}")
    else:
        print(f"inget varde kunde faststallas ({summary.error})")
    if summary.frames_dir:
        print(f"bilder        : {summary.frames_dir}")
    return 0 if summary.value else 1


def cmd_peek(cfg: Config, args: argparse.Namespace) -> int:
    """Sparar en farsk bild och ett forstorat utsnitt av displayen.

    Bra nar ROI:t ska justeras: titta pa captures/peek.png och flytta
    CALIBRATION_ROI i .env tills utsnittet sitter runt siffrorna.
    """
    camera = HikvisionCamera(cfg.camera)
    try:
        frame = camera.snapshot()
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    image = frame.image
    height, width = image.shape[:2]
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    snapshot_path = DEBUG_DIR / "last_snapshot.jpg"
    snapshot_path.write_bytes(frame.jpeg)

    if args.roi:
        roi = tuple(int(v) for v in args.roi.split(","))
    elif cfg.calibration_roi:
        roi = cfg.calibration_roi
    else:
        print("FEL: ingen ROI angiven. Satt CALIBRATION_ROI i .env eller skicka --roi")
        return 2

    x1, y1 = max(0, roi[0]), max(0, roi[1])
    x2, y2 = min(width, roi[2]), min(height, roi[3])
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        print(f"FEL: ROI {roi} ligger utanfor bilden {width}x{height}")
        return 2

    big = cv2.resize(crop, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_CUBIC)
    out = DEBUG_DIR / "peek.png"
    cv2.imwrite(str(out), big)

    print(f"bild : {width}x{height} px -> {snapshot_path}")
    print(f"ROI  : {roi}  ({x2 - x1}x{y2 - y1} px)")
    print(f"{args.scale:g}x forstoring -> {out}  ({big.shape[1]}x{big.shape[0]} px)")
    return 0


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    """Foljer displayen live och visar den som text i terminalen."""
    import subprocess

    script = ROOT / "tools" / "watch_display.py"
    roi = args.roi or _roi_arg_from_calibration(cfg)
    return subprocess.call(
        [
            sys.executable,
            str(script),
            "--minutes",
            str(args.minutes),
            "--interval",
            str(args.interval),
            "--roi",
            roi,
        ]
    )


def _roi_arg_from_calibration(cfg: Config) -> str:
    cal = Calibration.load(cfg.calibration_file)
    return ",".join(str(v) for v in cal.roi)


def cmd_lamp(cfg: Config, args: argparse.Namespace) -> int:
    ha = HomeAssistant(cfg.ha)
    if not ha.has_lamp:
        print("FEL: HA_LIGHT_ENTITY ar inte satt i .env")
        return 2

    try:
        if args.action == "on":
            print("tande lampan" if ha.lamp_on() else "ingen lampa konfigurerad")
        elif args.action == "off":
            print("slackte lampan" if ha.lamp_off() else "ingen lampa konfigurerad")
        else:
            state = ha.get_state(cfg.ha.light_entity)
            print(f"{cfg.ha.light_entity} = {state.get('state')}")
            print(json.dumps(state.get("attributes", {}), indent=2, ensure_ascii=False)[:600])
    except HaError as exc:
        print(f"FEL: {exc}")
        return 1
    return 0


def cmd_mqtt_test(cfg: Config, args: argparse.Namespace) -> int:
    """Publicerar ett provvarde sa att sensorerna dyker upp i HA."""
    from display_reader import Consensus
    from mqtt_publisher import MqttPublisher

    publisher = MqttPublisher(cfg.mqtt)
    if not publisher.connect():
        print("FEL: kunde inte ansluta till MQTT. Kontrollera MQTT_HOST i .env")
        return 1

    value = args.value
    result = Consensus(value=str(value), votes=5, total=5, confidence=0.99)
    publisher.publish_result(result, extra={"test": True})
    publisher.disconnect()

    print(f"publicerade provvarde {value} till {cfg.mqtt.base_topic}/state")
    print("Sensorerna borde nu dyka upp i Home Assistant under enheten 'Vattenkamera (pumpen)'.")
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from pipeline import NightlyRunner

    runner = NightlyRunner(cfg, use_lamp=not args.utan_lampa)
    summary = runner.run_once(duration_s=args.seconds, save=not args.utan_sparning)
    print(summary.to_json())
    return 0 if summary.value else 1


def cmd_daemon(cfg: Config, args: argparse.Namespace) -> int:
    from pipeline import NightlyRunner

    runner = NightlyRunner(cfg, use_lamp=not args.utan_lampa)
    target = runner.next_run_time()
    print(f"nasta korning: {target:%Y-%m-%d %H:%M:%S} (startar {cfg.run.pre_start_s:.0f} s innan)")
    print("avbryt med Ctrl+C")
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\navslutar")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vatten_kamera",
        description="Laser pumpdisplayen efter 02:00 och skickar vardet till Home Assistant.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="visa debug-loggning")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="visa version och konfiguration").set_defaults(func=cmd_version)
    sub.add_parser("probe", help="testa kameran").set_defaults(func=cmd_probe)

    cal = sub.add_parser("calibrate", help="hitta displayen och kalibrera rutnatet")
    cal.add_argument("--roi", help="x1,y1,x2,y2 (annars foreslas ett)")
    cal.add_argument("--digits", type=int, help="antal siffror pa displayen")
    cal.add_argument("--save", action="store_true", help="spara kalibreringen")
    cal.set_defaults(func=cmd_calibrate)

    read = sub.add_parser("read", help="las displayen nu")
    read.add_argument("--seconds", type=float, default=15.0, help="hur lange vi laser")
    read.add_argument("--lampa", action="store_true", help="tand lampan under lasningen")
    read.add_argument("--spara", action="store_true", help="spara bilderna")
    read.set_defaults(func=cmd_read)

    watch = sub.add_parser("watch", help="folj displayen live i terminalen")
    watch.add_argument("--minutes", type=float, default=2.0)
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--roi", help="x1,y1,x2,y2")
    watch.set_defaults(func=cmd_watch)

    peek = sub.add_parser("peek", help="spara ett forstorat utsnitt av displayen")
    peek.add_argument("--roi", help="x1,y1,x2,y2 (annars CALIBRATION_ROI ur .env)")
    peek.add_argument("--scale", type=float, default=4.0, help="forstoring")
    peek.set_defaults(func=cmd_peek)

    lamp = sub.add_parser("lamp", help="tanda, slacka eller las av lampan")
    lamp.add_argument("action", choices=["on", "off", "state"])
    lamp.set_defaults(func=cmd_lamp)

    mqtt = sub.add_parser("mqtt-test", help="publicera ett provvarde till HA")
    mqtt.add_argument("--value", type=int, default=1050)
    mqtt.set_defaults(func=cmd_mqtt_test)

    run = sub.add_parser("run", help="en komplett korning direkt")
    run.add_argument("--seconds", type=float, help="langd pa lasfonstret")
    run.add_argument("--utan-lampa", action="store_true")
    run.add_argument("--utan-sparning", action="store_true")
    run.set_defaults(func=cmd_run)

    daemon = sub.add_parser("daemon", help="vanta in klockslaget och kor varje natt")
    daemon.add_argument("--utan-lampa", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"FEL: kunde inte lasa konfigurationen: {exc}")
        return 2

    try:
        return int(args.func(cfg, args))
    except FileNotFoundError as exc:
        print(f"FEL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
