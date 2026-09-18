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
import time
from datetime import datetime

import cv2
import numpy as np

from camera import CameraError, HikvisionCamera
from config import ROOT, VERSION, Config, load_config
from display_reader import (
    Calibration,
    decode_cell,
    detect_cells,
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


def _tighten_roi(
    roi: tuple[int, int, int, int],
    band: tuple[int, int, int, int],
    cfg: Config,
    shape: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Drar at ROI:t sa att det precis omsluter sifferbandet.

    Blir ROI:t for stort hamnar tomt morker och pumphus med i bade troskling och
    normalisering, och da blir siffrorna samre atergivna. Vi drar darfor at
    utsnittet runt det band vi hittade och analyserar om.
    """
    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    height, width = shape

    x1 = int(round(roi[0] + band[0] / scale))
    y1 = int(round(roi[1] + band[1] / scale))
    x2 = int(round(roi[0] + band[2] / scale))
    y2 = int(round(roi[1] + band[3] / scale))

    pad_x = max(3, int(0.12 * (x2 - x1)))
    pad_y = max(2, int(0.25 * (y2 - y1)))

    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(width, x2 + pad_x),
        min(height, y2 + pad_y),
    )


def _pick_calibration_frame(
    frames: list[object],
    roi: tuple[int, int, int, int],
    cfg: Config,
) -> object | None:
    """Valjer den bild som visar flest siffror samtidigt.

    Displayen vaxlar mellan olika vyer - klockan visar alla sifferpositioner,
    medan ett vardepage visar farre. Kalibrerar vi mot en bild dar bara nagra
    siffror lyser blir rutnatet for litet och hamnar fel sa fort displayen visar
    nagot annat. Vi valjer darfor den bild som visar mest.
    """
    from display_reader import detect_cells_by_blobs

    best_frame = None
    best_score = -1.0

    for frame in frames:
        image = getattr(frame, "image", None)
        if image is None:
            continue
        try:
            _normalized, binary = preprocess(image, roi, cfg.reader)
            band = find_band(binary)
        except Exception:  # noqa: BLE001 - en trasig bild ska inte stoppa kalibreringen
            continue

        cells = detect_cells_by_blobs(binary, band, cfg.reader.digit_count)
        lit = float(np.count_nonzero(binary))
        # Fler siffror ar alltid battre; lika manga -> den bild med mest ljus.
        score = (len(cells) * 1e9 + lit) if cells else lit
        if score > best_score:
            best_score = score
            best_frame = frame

    return best_frame or (frames[-1] if frames else None)


def cmd_calibrate(cfg: Config, args: argparse.Namespace) -> int:
    """Hittar displayen i en bild och sparar ett kalibrerat rutnat.

    Displayen vaxlar varde hela tiden, och en siffra som rakar vara slackt just
    nu syns inte alls. Vi tar darfor ett antal bilder och behaller det ljusaste
    vardet per pixel. Da framtrader alla sifferpositioner samtidigt, oavsett
    vilket varde som rakade visas.
    """
    camera = HikvisionCamera(cfg.camera)
    frames = []
    try:
        for index in range(max(1, args.frames)):
            frames.append(camera.snapshot())
            if index < args.frames - 1:
                time.sleep(args.interval)
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    image = frames[-1].image
    height, width = image.shape[:2]
    print(f"bild: {width}x{height} px")

    if len(frames) > 1:
        chosen = _pick_calibration_frame(frames, cfg.calibration_roi or (0, 0, width, height), cfg)
        image = getattr(chosen, "image", image)
        print(f"valde den bild av {len(frames)} som visar flest siffror")
        analysis_image = image
    else:
        analysis_image = image

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
        roi = _suggest_roi(analysis_image, cfg)
        print(f"foreslaget ROI: {roi[0]},{roi[1]},{roi[2]},{roi[3]}")

    digits = args.digits or cfg.reader.digit_count

    # Rita en hjalpbild sa man ser att ROI:t sitter ratt.
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    marked = image.copy()
    cv2.rectangle(marked, (roi[0], roi[1]), (roi[2], roi[3]), (0, 0, 255), 4)
    marked_path = DEBUG_DIR / f"calibrate_{stamp}_roi.png"
    cv2.imwrite(str(marked_path), marked)

    if len(frames) > 1:
        cv2.imwrite(str(DEBUG_DIR / f"calibrate_{stamp}_stack.png"), analysis_image[roi[1] : roi[3], roi[0] : roi[2]])

    normalized, binary = preprocess(analysis_image, roi, cfg.reader)
    band = find_band(binary)

    # Dra at utsnittet runt det band vi hittade och analysera om. Da slipper man
    # sitta och passa in ROI:t for hand, och tolkningen blir sakrare nar varken
    # tomt morker eller pumphus kommer med.
    tightened = _tighten_roi(roi, band, cfg, (height, width))
    # Bara om utsnittet ar mycket storre an sjalva siffrorna. Ar det redan natt
    # och tight ar en atstramning bara riskabel - den kan skara bort siffror.
    band_scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    band_area = max(1.0, float((band[2] - band[0]) * (band[3] - band[1])))
    roi_area = max(1.0, float((roi[2] - roi[0]) * (roi[3] - roi[1]))) * band_scale * band_scale
    if tightened != roi and band_area < 0.45 * roi_area:
        print(f"drar at utsnittet: {roi} -> {tightened}")
        roi = tightened
        normalized, binary = preprocess(analysis_image, roi, cfg.reader)
        band = find_band(binary)
    else:
        print("utsnittet ar redan tight - behaller det")

    detected = detect_cells(normalized, binary, band, digits)
    fitted, fitted_score = fit_grid(normalized, binary, band, digits)

    from display_reader import score_boxes

    # Ett fast sjusegmentsblock har jamnt fordelade sifferfonster, och dar ar en
    # jamn delning av bandet det mest tillforlitliga: dalarna mellan siffrorna
    # ar smala och latta att hamna fel pa (halet inne i en nolla ar ocksa
    # morkt), och rutnatssokningen belonar dessutom breda celler eftersom de
    # ger en sjalvsaker men felaktig "atta". Den jamna delningen traffar ratt sa
    # lange alla positioner lyser, sa den provas forst. Dalarna och sokningen
    # anvands bara om den skulle fa en riktigt dalig poang.
    band_x1, band_y1, band_x2, band_y2 = band
    span = band_x2 - band_x1
    even = [
        (
            band_x1 + int(round(index * span / digits)),
            band_y1,
            band_x1 + int(round((index + 1) * span / digits)),
            band_y2,
        )
        for index in range(digits)
    ]
    even_score = score_boxes(normalized, binary, band, even)

    if even_score >= 0.0:
        boxes = even
        grid_score = even_score
        print("jamn delning av bandet (fast sjusegmentsblock)")
    elif detected is not None:
        boxes = detected
        grid_score = score_boxes(normalized, binary, band, detected)
        print("cellerna hittades via mellanrummen mellan siffrorna")
    else:
        boxes = fitted
        grid_score = fitted_score
        print("mellanrummen gick inte att hitta - rutnatet passades in med sokning")

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

    # Läs den senaste bilden med rutnatet - tidsstacken visar flera varden och
    # gar inte att tolka som ett tal.
    reading = read_image(image, calibration, cfg.reader, timestamp=frames[-1].timestamp)

    print(f"\nsifferband : {band}")
    if grid_score == grid_score:  # inte NaN
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

    # Rita ut de sju samplingsrutorna per siffra ovanpa den bild avlasaren ser.
    # Gron ruta = avlasaren tycker att segmentet lyser. Da syns det direkt om
    # rutorna sitter pa segmenten eller bredvid.
    from segments import SEGMENT_BOXES

    canvas = cv2.cvtColor((normalized * 255).astype("uint8"), cv2.COLOR_GRAY2BGR)
    for cell, digit in zip(boxes, reading.digits):
        cx1, cy1, cx2, cy2 = cell
        cell_w = cx2 - cx1
        cell_h = cy2 - cy1
        for name, (fx1, fy1, fx2, fy2) in SEGMENT_BOXES.items():
            sx1 = cx1 + int(round(fx1 * cell_w))
            sx2 = cx1 + int(round(fx2 * cell_w))
            sy1 = cy1 + int(round(fy1 * cell_h))
            sy2 = cy1 + int(round(fy2 * cell_h))
            lit = digit.segment_values.get(name, 0.0) > 0.5
            colour = (0, 255, 0) if lit else (0, 0, 255)
            cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), colour, 1)
    segments_path = DEBUG_DIR / f"calibrate_{stamp}_segments.png"
    cv2.imwrite(str(segments_path), canvas)
    print(f"segmentrutor -> {segments_path}  (gron = lyser, rod = slackt)")

    if args.save:
        # Spara samma normaliserade ROI-bild som avlasaren ser, sa att varje
        # senare bildruta kan riktas in mot den innan cellerna anvands. Det gor
        # lasningen okanslig for att kameran rubbas nagra pixel.
        from display_reader import REFERENCE_FILE, save_reference

        reference_path = cfg.calibration_file.with_name(REFERENCE_FILE)
        save_reference(normalized, str(reference_path))
        print(f"referensbild    -> {reference_path}")

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
    i kameran, i ljuset eller i sjalva tolkningen.
    """
    from camera_settings import CameraSettings, CameraSettingsError
    from display_reader import detect_cells as detect, score_boxes

    camera = HikvisionCamera(cfg.camera)
    frames = []
    try:
        for index in range(max(1, args.frames)):
            frames.append(camera.snapshot())
            if index < args.frames - 1:
                time.sleep(1.0)
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    image = np.max(np.stack([f.image for f in frames]), axis=0) if len(frames) > 1 else frames[-1].image
    if not cfg.calibration_roi:
        print("FEL: CALIBRATION_ROI maste vara satt i .env")
        return 2
    roi = cfg.calibration_roi

    normalized, binary = preprocess(image, roi, cfg.reader)
    band = find_band(binary)

    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    row_width = (band[2] - band[0]) / scale
    row_height = (band[3] - band[1]) / scale
    per_digit = row_width / cfg.reader.digit_count

    boxes = detect(normalized, binary, band, cfg.reader.digit_count)
    method = "mellanrummen mellan siffrorna"
    if boxes is None:
        boxes, _ = fit_grid(normalized, binary, band, cfg.reader.digit_count)
        method = "inpassning med sokning"
    score = score_boxes(normalized, binary, band, boxes)

    print(f"version        : {VERSION}")
    print(f"bild           : {image.shape[1]}x{image.shape[0]} px, {len(frames)} bilder i stacken")
    print(f"utsnitt (ROI)  : {roi[0]},{roi[1]},{roi[2]},{roi[3]}")
    print(f"sifferband     : {row_width:.0f}x{row_height:.0f} px   ({per_digit:.1f} px per siffra)")
    print(f"cellindelning  : {method}")
    print(f"lasbarhetspoang: {score:+.3f}")

    print("\nsiffrorna just nu (ur tidsstacken - alla positioner samtidigt):")
    print("(en tidsstack visar allt som lyst under körningen, sa en siffra kan se")
    print(" ut som en atta trots att den aldrig visat en atta - det ar vantan)")
    for index, box in enumerate(boxes, start=1):
        cell = normalized[box[1] : box[3], box[0] : box[2]]
        result = decode_cell(cell) if cell.size else None
        if result is None:
            continue
        print(f"  siffra {index}: {result.char!r} konfidens {result.confidence:.2f}")

    try:
        settings = CameraSettings(cfg.camera)
        values = settings.read()
        print("\nkameran:")
        for key in ("ircut", "exposure_type", "gain", "shutter", "sharpness", "wdr_mode"):
            if key in values:
                print(f"  {key:<14} {values[key]}")
        if values.get("ircut") == "night":
            print("  -> NATTLAGE: displayen branner ut. Kor: main.py image set ircut=day")
    except CameraSettingsError as exc:
        print(f"kameran: kunde inte lasas ({exc})")

    print("\nbedomning:")
    ok = True
    if per_digit < 25:
        print(f"  FOR LITEN: bara {per_digit:.0f} px per siffra. Segmenten gar inte att skilja.")
        print("             Flytta kameran namare - sikta pa minst 30 px per siffra")
        print(f"             (alltsa {30 * cfg.reader.digit_count:.0f} px for hela raden).")
        ok = False
    elif per_digit < 30:
        print(f"  PA GRANSEN: {per_digit:.0f} px per siffra. Kan ga, men osakert.")
    else:
        print(f"  BRA: {per_digit:.0f} px per siffra.")

    if score < 0.30:
        print(f"  LASBARHETEN AR LAG ({score:+.3f}). Kontrollera exponering och troskel.")
        ok = False

    if ok:
        print("  Allt ser bra ut. Kor 'main.py calibrate --save' och sedan 'main.py read'.")

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
    from display_reader import find_band, fit_grid, preprocess

    if not cfg.calibration_roi:
        print("FEL: CALIBRATION_ROI maste vara satt i .env for att kunna optimera")
        return 2

    roi = cfg.calibration_roi
    digits = cfg.reader.digit_count
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
                    normalized, binary = preprocess(frame.image, roi, cfg.reader)
                    band = find_band(binary)
                    _boxes, score = fit_grid(normalized, binary, band, digits)
                except ReaderError as exc:
                    print(f"  gain={gain:<4} slutare={shutter:<6} kunde inte tolka: {exc}")
                    continue

                band_size = f"{band[2] - band[0]}x{band[3] - band[1]}"
                results.append((score, gain.strip(), shutter.strip(), band_size))
                print(
                    f"  gain={gain:<4} slutare={shutter:<6} lasbarhet {score:+.3f}"
                    f"   sifferband {band_size} px"
                )
    finally:
        camera.close()

    if not results:
        print("FEL: ingen kombination gick att prova")
        return 1

    results.sort(key=lambda item: item[0], reverse=True)
    best_score, best_gain, best_shutter, band_size = results[0]
    print(f"\nbasta: gain={best_gain} slutare={best_shutter} (lasbarhet {best_score:+.3f}, band {band_size} px)")

    settings.apply({"gain": best_gain, "shutter": best_shutter})  # type: ignore[attr-defined]
    print("installt. Kor 'main.py calibrate --save' for att passa in rutnatet.")
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
    cal.add_argument("--frames", type=int, default=8, help="antal bilder i tidsstacken")
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
