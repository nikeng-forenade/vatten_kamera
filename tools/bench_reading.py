"""Mater vad en lasning kostar i CPU - utan kamera och utan vantan.

Korningen tar en bild i sekunden i tva minuter, men det ar vantetiden som ar
lang, inte raknandet. Det har verktyget kor samma bildvag som pipeline.py
(avkoda JPEG -> klipp ut ROI -> tolka siffrorna -> gruppera) pa bilder som redan
ligger pa disk, och visar vad en hel natt kostar i CPU-tid och minne.

Kor:
    python tools/bench_reading.py
    python tools/bench_reading.py captures/series/s3 --frames 120
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CAPTURES_DIR, ROOT, load_config  # noqa: E402
from display_reader import (  # noqa: E402
    Calibration,
    crop_roi,
    group_similar,
    image_from_crop,
    read_image,
    typical_crops,
)


def newest_run() -> Path | None:
    runs = sorted((CAPTURES_DIR / "runs").glob("20*"))
    return runs[-1] if runs else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Vad kostar en lasning i CPU?")
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        help="mapp med bilder (annars senaste korningen)",
    )
    parser.add_argument("--frames", type=int, default=120, help="antal bilder i en nattlig korning")
    args = parser.parse_args()

    directory = args.directory or newest_run()
    if directory is None or not directory.is_dir():
        print("FEL: ingen mapp med bilder - kor en lasning forst (main.py read --spara)")
        return 2

    cfg = load_config()
    calibration = Calibration.load(cfg.calibration_file)

    paths = sorted(directory.glob("*.jpg"))
    if not paths:
        print(f"FEL: inga bilder i {directory}")
        return 2

    payloads = [path.read_bytes() for path in paths]
    size = cv2.imdecode(np.frombuffer(payloads[0], np.uint8), cv2.IMREAD_COLOR).shape[:2]

    # 1. Avkoda JPEG: det som kameran skickar och det tyngsta steget.
    decode_ms: list[float] = []
    images = []
    for payload in payloads:
        started = time.perf_counter()
        image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
        decode_ms.append((time.perf_counter() - started) * 1000)
        images.append(image)

    # 2. Klipp ut ROI:t och valj fargkanal.
    crop_ms: list[float] = []
    crops = []
    for image in images:
        started = time.perf_counter()
        crops.append(crop_roi(image, calibration.roi, cfg.reader.channel))
        crop_ms.append((time.perf_counter() - started) * 1000)

    # 3. Tolka en typisk bild ur varje grupp - precis som korningen gor.
    read_ms: list[float] = []
    started = time.perf_counter()
    groups = group_similar(crops, cfg.run.group_threshold)
    group_ms = (time.perf_counter() - started) * 1000

    for indices in groups:
        typical = typical_crops(crops, indices)
        image = image_from_crop(typical, calibration.roi)
        step = time.perf_counter()
        read_image(image, calibration, cfg.reader, timestamp=float(len(indices)))
        read_ms.append((time.perf_counter() - step) * 1000)

    per_frame = statistics.median(decode_ms) + statistics.median(crop_ms)
    per_group = statistics.median(read_ms)
    night = (per_frame * args.frames) / 1000.0 + (per_group * len(groups)) / 1000.0
    scale = cfg.reader.upscale or 1.0
    roi_px = (calibration.roi[2] - calibration.roi[0], calibration.roi[3] - calibration.roi[1])

    print(f"bilder          : {len(paths)} i {directory}  ({size[1]}x{size[0]} JPEG)")
    print(f"avkoda JPEG     : {statistics.median(decode_ms):.1f} ms/bild")
    print(f"klipp ROI       : {statistics.median(crop_ms):.1f} ms/bild  (utsnitt {roi_px[0]}x{roi_px[1]} px)")
    print(f"gruppera {len(crops):>3}     : {group_ms:.0f} ms en gang")
    print(f"tolka en sida   : {per_group:.0f} ms  ({len(groups)} grupper)")
    print()
    print(f"en nattlig korning pa {args.frames} bilder: {night:.1f} s CPU pa EN karna")
    print(f"  varav avkodning {per_frame * args.frames / 1000.0:.1f} s och tolkning {per_group * len(groups) / 1000.0:.1f} s")
    print()
    print(f"minne, raknat:")
    print(f"  en bild i minnet          : {size[0] * size[1] * 3 / 1e6:.0f} MB")
    print(f"  utsnittet forstorat {scale:g}x  : {roi_px[0] * scale * roi_px[1] * scale * 4 / 1e6:.0f} MB per bild")
    print(f"  alla utsnitt ({args.frames} st)   : {roi_px[0] * roi_px[1] * 4 * args.frames / 1e6:.0f} MB")
    print()
    print("ingen GPU anvands: allt ar numpy och OpenCV pa CPU.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
