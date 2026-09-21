"""Kommandoradsverktyg for vattenkameran.

Exempel:
    python main.py version
    python main.py probe                     # testa kameran
    python main.py calibrate --frames 16     # mater sifferpositionerna
    python main.py calibrate --frames 16 --save
    python main.py calibrate --roi 985,0,1620,200 --digits 4
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
import time
from datetime import datetime

import cv2
import numpy as np

from camera import CameraError, HikvisionCamera
from config import ROOT, VERSION, Config, load_config
from display_reader import (
    Calibration,
    ReaderError,
    draw_cells_overlay,
    load_cell_boxes,
    measure_cells,
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
    """Mater siffercellerna och sparar kalibreringen.

    Displayen vaxlar mellan olika vyer, sa en enskild bild visar bara nagra av
    sifferpositionerna. Vi tar darfor en serie bilder och mater varje position
    for sig: kameran ser panelen snett, sa sifferraden lutar och den sista
    siffran sitter nagra tiotal pixlar lagre i bilden an den forsta.
    """
    camera = HikvisionCamera(cfg.camera)
    frames: list[np.ndarray] = []
    try:
        for index in range(max(1, args.frames)):
            frames.append(camera.snapshot().image)
            if index < args.frames - 1:
                time.sleep(args.interval)
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    image = frames[-1]
    height, width = image.shape[:2]
    print(f"bild: {width}x{height} px, {len(frames)} bilder i serien")

    if args.roi:
        roi = tuple(int(v) for v in args.roi.split(","))
        if len(roi) != 4:
            print("FEL: --roi ska vara x1,y1,x2,y2")
            return 2
    elif cfg.calibration_roi:
        roi = cfg.calibration_roi
        print(f"ROI fran .env: {roi[0]},{roi[1]},{roi[2]},{roi[3]}")
    else:
        roi = _suggest_roi(image, cfg)
        print(f"foreslaget ROI (saltt i .env): {roi[0]},{roi[1]},{roi[2]},{roi[3]}")

    digits = args.digits or cfg.reader.digit_count

    try:
        absolute, report = measure_cells(
            frames, roi, cfg.reader, digits, prior=load_cell_boxes(cfg.calibration_file)
        )
    except ReaderError as exc:
        print(f"FEL: {exc}")
        return 1

    print()
    for line in report:
        print(f"  {line}")

    print("\ncellrutor i helbildens koordinater:")
    for index, box in enumerate(absolute, start=1):
        print(f"  position {index}: x {box[0]:4d}..{box[2]:4d} ({box[2] - box[0]:3d} px)"
              f"   y {box[1]:4d}..{box[3]:4d} ({box[3] - box[1]:3d} px)")

    # Kontrollbilder: hela bilden med cellrutorna, och tidsstacken (dar alla
    # positioner lyser samtidigt) med cellrutor och matfonster. Sitter de grona
    # rutorna pa segmenten ar geometrin ratt.
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    marked = image.copy()
    cv2.rectangle(marked, (roi[0], roi[1]), (roi[2], roi[3]), (0, 0, 255), 4)
    for box in absolute:
        cv2.rectangle(marked, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 2)
    marked_path = DEBUG_DIR / f"calibrate_{stamp}_roi.png"
    cv2.imwrite(str(marked_path), marked)

    stacked = np.max(np.stack(frames), axis=0)
    cv2.imwrite(
        str(DEBUG_DIR / f"calibrate_{stamp}_stack.png"),
        stacked[roi[1] : roi[3], roi[0] : roi[2]],
    )
    segments_path = DEBUG_DIR / f"calibrate_{stamp}_segments.png"
    cv2.imwrite(str(segments_path), draw_cells_overlay(stacked, roi, absolute, cfg.reader))
    print(f"\nkontrollbilder -> {DEBUG_DIR}\\calibrate_{stamp}_roi.png och _segments.png")

    calibration = Calibration(roi=tuple(roi), digit_count=digits, cell_boxes=absolute)
    reading = read_image(image, calibration, cfg.reader)
    print(f"laser just nu : {reading.value!r}   konfidens {reading.confidence:.2f}")
    for index, digit in enumerate(reading.digits, start=1):
        values = " ".join(f"{key}={value:.2f}" for key, value in sorted(digit.segment_values.items()))
        print(f"  position {index}: {digit.char!r} konfidens {digit.confidence:.2f}   {values}")

    if args.save:
        cfg.calibration_file.write_text(
            json.dumps(
                {
                    "roi": list(roi),
                    "digit_count": digits,
                    "cell_boxes": [list(box) for box in absolute],
                    "notes": f"kalibrerad {datetime.now():%Y-%m-%d %H:%M} - egen hojd per position",
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nkalibrering sparad -> {cfg.calibration_file}")

        if cfg.reader.reference_file:
            # Referensbilden anvands for att rikta in senare bildrutor. Den tas
            # fran tidsstacken, sa att den visar alla positioner samtidigt.
            from display_reader import save_reference

            normalized, _binary = preprocess(stacked, roi, cfg.reader)
            reference_path = cfg.calibration_file.with_name(cfg.reader.reference_file)
            save_reference(normalized, str(reference_path))
            print(f"referensbild       -> {reference_path}")
    else:
        print("\n(torrkorning - inget sparat. Kor med --save for att spara)")

    print(f"kontrollbild       -> {marked_path}")
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
        print("FEL: ingen kalibrering finns. Kor 'main.py calibrate --frames 16 --save' forst.")
        return 1

    print()
    print(f"bilder lasta  : {summary.frames_taken}")
    print(f"lasbara bilder: {summary.frames_readable}")
    rostade = [item for item in summary.details if item.get("rostad")]
    if rostade:
        print(
            f"rostar pa     : vardesidan efter 02:00 - "
            f"{len(rostade)} grupper, {sum(item['bilder'] for item in rostade)} bilder"
        )
    if summary.page_note:
        print(f"rostning      : {summary.page_note}")
    if summary.value:
        # Skriv vardet med displayens decimaler: "090" ar 0.90, inte 1.
        decimals = cfg.reader.decimals
        shown = f"{summary.numeric:.{decimals}f}" if summary.numeric is not None else "?"
        print(f"VARDET        : {shown}  (text {summary.value!r})")
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

    big = cv2.resize(
        crop,
        None,
        fx=args.scale,
        fy=args.scale,
        interpolation=cv2.INTER_NEAREST if args.nearest else cv2.INTER_CUBIC,
    )
    out = DEBUG_DIR / ("peek_raw.png" if args.nearest else "peek.png")
    cv2.imwrite(str(out), big)

    # Visa vad avlasaren faktiskt tittar pa: vilken fargkanal som valjs och hur
    # den ser ut. Det ar den bild all tolkning utgar ifran.
    from display_reader import channel_spreads, pick_channel

    spreads = channel_spreads(crop)
    gray, chosen = pick_channel(crop, cfg.reader.channel)
    print("kanaler   : " + "  ".join(f"{name}={value:.0f}" for name, value in spreads.items()))
    print(f"avlasaren : anvander {chosen!r} (CONTRAST-spann)")

    low, high = float(np.percentile(gray, 2)), float(np.percentile(gray, 99.5))
    stretched = np.clip((gray - low) * (255.0 / max(1.0, high - low)), 0, 255).astype(np.uint8)
    gray_big = cv2.resize(
        stretched,
        None,
        fx=args.scale,
        fy=args.scale,
        interpolation=cv2.INTER_NEAREST if args.nearest else cv2.INTER_CUBIC,
    )
    gray_path = DEBUG_DIR / "peek_channel.png"
    cv2.imwrite(str(gray_path), gray_big)
    print(f"kanalbild : {gray_path}")

    print(f"bild : {width}x{height} px -> {snapshot_path}")
    print(f"ROI  : {roi}  ({x2 - x1}x{y2 - y1} px)")
    print(f"{args.scale:g}x forstoring -> {out}  ({big.shape[1]}x{big.shape[0]} px)")
    return 0


def cmd_diagnose(cfg: Config, args: argparse.Namespace) -> int:
    """Sager till om kameran star tillrackligt nara for att kunna lasa displayen.

    Kor den har forst nar nagot inte fungerar: den svarar pa om problemet sitter
    i kameran, i ljuset eller i sjalva tolkningen. Den laser de sparade
    cellrutorna och nagra enskilda bilder, sa att svaret sager nagot om hur
    lasningen faktiskt ser ut - inte bara hur en tidsstack ser ut.
    """
    from camera_settings import CameraSettings, CameraSettingsError
    from segments import segment_values

    camera = HikvisionCamera(cfg.camera)
    frames: list[np.ndarray] = []
    try:
        for index in range(max(1, args.frames)):
            frames.append(camera.snapshot().image)
            if index < args.frames - 1:
                time.sleep(1.0)
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    print(f"version        : {VERSION}")
    print(f"bild           : {frames[-1].shape[1]}x{frames[-1].shape[0]} px, {len(frames)} bilder")

    if not cfg.calibration_file.exists():
        print("FEL: ingen kalibrering. Kor 'main.py calibrate --frames 16 --save' forst.")
        return 2

    try:
        calibration = Calibration.load(cfg.calibration_file)
    except ReaderError as exc:
        print(f"FEL: {exc}")
        return 2

    roi = calibration.roi
    print(f"utsnitt (ROI)  : {roi[0]},{roi[1]},{roi[2]},{roi[3]}")

    if not calibration.cell_boxes:
        print("FEL: kalibreringen saknar cellrutor. Kor 'main.py calibrate --frames 16 --save'.")
        return 2

    widths = [box[2] - box[0] for box in calibration.cell_boxes]
    heights = [box[3] - box[1] for box in calibration.cell_boxes]
    print(f"celler         : {min(widths)}-{max(widths)} px breda, {min(heights)}-{max(heights)} px hoga")
    for index, box in enumerate(calibration.cell_boxes, start=1):
        print(f"  position {index}   : x {box[0]}..{box[2]}   y {box[1]}..{box[3]}")

    # Las de sista bilderna var for sig. En tidsstack visar allt som lyst under
    # korningen, sa en siffra kan se ut som en atta dar fast den aldrig visat en
    # atta - bara riktiga bilder sager nagot om lasningen.
    print("\nlasning av de sista bilderna:")
    tail = frames[-min(4, len(frames)) :]
    values: list[str] = []
    for index, image in enumerate(tail, start=1):
        reading = read_image(image, calibration, cfg.reader)
        digits = " ".join(f"{digit.char!r}:{digit.confidence:.2f}" for digit in reading.digits)
        print(f"  bild {index}: varde {reading.value!r:<8} konfidens {reading.confidence:.2f}   {digits}")
        if reading.ok:
            values.append(reading.value)

    stacked = np.max(np.stack(frames), axis=0) if len(frames) > 1 else frames[-1]
    normalized, _binary = preprocess(stacked, roi, cfg.reader)
    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    image_height, image_width = normalized.shape[:2]
    print("\nsegment ur tidsstacken (alla positioner samtidigt):")
    for index, (ax1, ay1, ax2, ay2) in enumerate(calibration.cell_boxes, start=1):
        cx1 = max(0, min(image_width, int(round((ax1 - roi[0]) * scale))))
        cy1 = max(0, min(image_height, int(round((ay1 - roi[1]) * scale))))
        cx2 = max(0, min(image_width, int(round((ax2 - roi[0]) * scale))))
        cy2 = max(0, min(image_height, int(round((ay2 - roi[1]) * scale))))
        cell = normalized[cy1:cy2, cx1:cx2]
        if cell.size == 0:
            print(f"  position {index}: cellen ligger utanfor bilden")
            continue
        values_by_segment = segment_values(cell)
        lit = "".join(name for name, value in sorted(values_by_segment.items()) if value > 0.5) or "-"
        print(f"  position {index}: lyser {lit:<8}"
              + "  " + " ".join(f"{key}={values_by_segment[key]:.2f}" for key in sorted(values_by_segment)))

    try:
        settings = CameraSettings(cfg.camera)
        camera_values = settings.read()
        print("\nkameran:")
        for key in ("ircut", "exposure_type", "gain", "shutter", "sharpness", "wdr_mode"):
            if key in camera_values:
                print(f"  {key:<14} {camera_values[key]}")
        if camera_values.get("ircut") == "night":
            print("  -> NATTLAGE: displayen branner ut. Kor: main.py image set ircut=day")
    except CameraSettingsError as exc:
        print(f"kameran: kunde inte lasas ({exc})")

    print("\nbedomning:")
    ok = True
    smallest = min(widths)
    if smallest < 60:
        print(f"  FOR LITEN: den smalaste cellen ar bara {smallest} px. Flytta kameran namare.")
        ok = False
    else:
        print(f"  cellstorleken ar bra ({smallest} px for den smalaste siffran).")

    if len(values) != len(tail):
        print(f"  bara {len(values)} av de {len(tail)} sista bilderna gav ett varde.")
        ok = False
    elif len(set(values)) == 1:
        print(f"  de sista bilderna visar samma varde ({values[0]!r}) - lasningen ar stabil.")
    else:
        print(f"  bilderna visar olika varden ({', '.join(repr(v) for v in values)}) -")
        print("  det ar normalt: displayen vaxlar mellan klockan, spoltiden och vardena.")

    if not ok:
        print("  Kor 'tools/fit_cells.py captures/<serie> --read' for att se fler bilder,")
        print("  eller 'main.py calibrate --frames 16 --save' om kameran flyttats.")

    return 0 if ok else 1


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


def cmd_image(cfg: Config, args: argparse.Namespace) -> int:
    """Laser, andrar eller aterstaller kamerans bildinstallningar."""
    from camera_settings import BACKUP_FILE, ALLOWED, PATTERNS, CameraSettings, CameraSettingsError

    settings = CameraSettings(cfg.camera)

    try:
        if args.action == "show":
            for key, value in settings.read().items():
                allowed = ALLOWED.get(key)
                hint = f"  (val: {', '.join(allowed)})" if allowed else ""
                print(f"  {key:<22} {value}{hint}")
            return 0

        if args.action == "save-profile":
            profile: dict[str, str] = {}
            for item in args.changes:
                if "=" not in item:
                    print(f"FEL: '{item}' ska skrivas nyckel=värde")
                    return 2
                key, value = item.split("=", 1)
                profile[key.strip()] = value.strip()

            path = settings.save_profile(profile or None)
            saved = settings.load_profile() or {}
            print(f"lasprofil sparad -> {path}")
            for key, value in saved.items():
                print(f"  {key:<22} {value}")
            print("\nkameran rordes inte. Laget anvands bara under lasningen,")
            print("och kamerans eget lage lags tillbaka direkt efterat.")
            return 0

        if args.action == "show-profile":
            profile = settings.load_profile()
            if not profile:
                print("ingen lasprofil sparad - kameran lamnas som den ar")
                return 0
            print("lasprofil (anvands bara under lasningen):")
            for key, value in profile.items():
                print(f"  {key:<22} {value}")
            print("\nkameran har nu:")
            current = settings.read()
            for key in profile:
                marker = "" if current.get(key) == profile[key] else "   <- andras vid lasning"
                print(f"  {key:<22} {current.get(key, '?')}{marker}")
            return 0

        if args.action == "backup":
            path = settings.backup()
            print(f"backup sparad -> {path}")
            return 0

        if args.action == "restore":
            values = settings.restore()
            print("aterstallt:")
            for key, value in values.items():
                print(f"  {key:<22} {value}")
            return 0

        if args.action == "set":
            if not args.changes:
                print("FEL: ange vad som ska andras, t.ex. gain=30 shutter=1/100")
                print(f"Giltiga nycklar: {', '.join(PATTERNS)}")
                return 2

            changes: dict[str, str] = {}
            for item in args.changes:
                if "=" not in item:
                    print(f"FEL: '{item}' ska skrivas nyckel=värde")
                    return 2
                key, value = item.split("=", 1)
                changes[key.strip()] = value.strip()

            if not BACKUP_FILE.exists():
                settings.backup()
                print(f"(sparade en backup av nuvarande installningar -> {BACKUP_FILE.name})")
            before = settings.read()
            print("andrar:")
            for key, value in changes.items():
                print(f"  {key:<22} {before.get(key, '?')} -> {value}")

            values = settings.apply(changes)
            print("\ngaller nu:")
            for key in changes:
                print(f"  {key:<22} {values.get(key, '?')}")
            return 0

        if args.action == "tune":
            return _tune_exposure(cfg, settings, args)

        print(f"FEL: okant kommando '{args.action}'")
        return 2
    except CameraSettingsError as exc:
        print(f"FEL: {exc}")
        return 1


def _tune_exposure(cfg: Config, settings: object, args: argparse.Namespace) -> int:
    """Provar olika exponeringar och mater vilken som ger mest lasbar bild.

    Måttet ar avlasarens egen rutnatspoang, alltsa hur val ett sjusegmentsrutnat
    forklarar bilden. Det ar ett arligare mått an att titta pa kontrast: en hog
    forstarkning gor siffrorna mattade och "tydliga" i ett histogram, men da
    smalter de ihop till en enda klass och gar inte att skilja at.
    """
    import time

    from camera_settings import CameraSettingsError
    from display_reader import to_internal
    from segments import segment_values

    if not cfg.calibration_roi:
        print("FEL: CALIBRATION_ROI maste vara satt i .env for att kunna optimera")
        return 2

    roi = cfg.calibration_roi
    digits = cfg.reader.digit_count
    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    calibration_cells = load_cell_boxes(cfg.calibration_file)
    if not calibration_cells or len(calibration_cells) != digits:
        print("FEL: ingen kalibrering. Kor 'main.py calibrate --frames 16 --save' forst -")
        print("     exponeringen mats mot de riktiga sifferrutorna.")
        return 2
    boxes = to_internal(calibration_cells, roi, scale)
    settings.backup()  # type: ignore[attr-defined]
    print(f"utgar fran ROI {roi[0]},{roi[1]},{roi[2]},{roi[3]}, {digits} siffror\n")

    gains = (args.gains.split(",") if args.gains else ["10", "20", "40", "60", "80"])
    shutters = args.shutters.split(",") if args.shutters else ["1/25", "1/60", "1/100"]

    camera = HikvisionCamera(cfg.camera)
    results: list[tuple[float, str, str, str]] = []

    try:
        for gain in gains:
            for shutter in shutters:
                try:
                    settings.apply({"gain": gain.strip(), "shutter": shutter.strip()})  # type: ignore[attr-defined]
                except CameraSettingsError as exc:
                    print(f"  gain={gain:<4} slutare={shutter:<6} hoppas over: {exc}")
                    continue

                time.sleep(0.4)  # lat kameran stalla in sig
                try:
                    frame = camera.snapshot()
                except CameraError as exc:
                    print(f"  gain={gain:<4} slutare={shutter:<6} ingen bild: {exc}")
                    continue

                try:
                    score = _crispness(frame.image, roi, cfg, boxes)
                except ReaderError as exc:
                    print(f"  gain={gain:<4} slutare={shutter:<6} kunde inte tolka: {exc}")
                    continue

                results.append((score, gain.strip(), shutter.strip(), ""))
                print(f"  gain={gain:<4} slutare={shutter:<6} skarpa {score:.3f}")

    finally:
        camera.close()

    if not results:
        print("FEL: ingen kombination gick att prova")
        return 1

    results.sort(key=lambda item: item[0], reverse=True)
    best_score, best_gain, best_shutter, _band_size = results[0]
    print(f"\nbasta: gain={best_gain} slutare={best_shutter} (skarpa {best_score:.3f})")

    settings.apply({"gain": best_gain, "shutter": best_shutter})  # type: ignore[attr-defined]
    print("installt. Kontrollera med 'tools/fit_cells.py captures/<serie> --read'.")
    return 0


def _crispness(image: np.ndarray, roi: tuple[int, int, int, int], cfg: Config, boxes: list) -> float:
    """Hur tydligt avlasaren kan se om varje segment lyser eller inte.

    Mattet ar hur langt varje uppmatt segmentvarde ligger fran mitten (0.5), dar
    tolken inte kan skilja tant fran slackt. Det ar oberoende av vilken siffra
    displayen rakar visa - en glodande kant som smetar in over troskeln drar ner
    mattet, oavsett om siffran ar en nolla eller en atta. Att i stallet mata
    "hur sjalvsaker tolken ar" vore missvisande: en utbrand bild ger matta
    segment och en mycket sjalvsaker, men felaktig, atta.
    """
    from segments import segment_values

    normalized, _binary = preprocess(image, roi, cfg.reader)
    height, width = normalized.shape[:2]

    scores: list[float] = []
    for cx1, cy1, cx2, cy2 in boxes:
        x1 = max(0, min(width, int(round(cx1))))
        y1 = max(0, min(height, int(round(cy1))))
        x2 = max(0, min(width, int(round(cx2))))
        y2 = max(0, min(height, int(round(cy2))))
        cell = normalized[y1:y2, x1:x2]
        if cell.size == 0:
            continue
        scores.extend(2.0 * abs(value - 0.5) for value in segment_values(cell).values())

    if not scores:
        raise ReaderError("cellerna ligger utanfor bilden")
    return float(np.mean(scores))


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
    cal.add_argument("--frames", type=int, default=16, help="antal bilder i serien")
    cal.add_argument("--interval", type=float, default=1.0, help="sekunder mellan bilderna")
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
    peek.add_argument(
        "--nearest",
        action="store_true",
        help="visa de rada pixlarna utan utjamning (bra for att bedoma skarpa)",
    )
    peek.set_defaults(func=cmd_peek)

    diagnose = sub.add_parser("diagnose", help="bedom om kameran star tillrackligt nara")
    diagnose.add_argument("--frames", type=int, default=5, help="antal bilder i tidsstacken")
    diagnose.set_defaults(func=cmd_diagnose)

    lamp = sub.add_parser("lamp", help="tanda, slacka eller las av lampan")
    lamp.add_argument("action", choices=["on", "off", "state"])
    lamp.set_defaults(func=cmd_lamp)

    mqtt = sub.add_parser("mqtt-test", help="publicera ett provvarde till HA")
    mqtt.add_argument("--value", type=int, default=1050)
    mqtt.set_defaults(func=cmd_mqtt_test)

    image = sub.add_parser("image", help="kamerans bildinstallningar")
    image.add_argument(
        "action",
        choices=["show", "backup", "set", "restore", "tune", "save-profile", "show-profile"],
    )
    image.add_argument(
        "changes",
        nargs="*",
        help="for 'set': nyckel=värde, t.ex. gain=30 shutter=1/100 ircut=auto",
    )
    image.add_argument("--gains", help="for 'tune': kommaseparerade varden, t.ex. 2,5,10,20")
    image.add_argument("--shutters", help="for 'tune': kommaseparerade varden, t.ex. 1/50,1/250")
    image.set_defaults(func=cmd_image)

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
