"""Visar hur rutnatet for siffrorna hamnar - for felsokning av avlasningen.

Kan kora pa en syntetisk siffra (for att testa logiken) eller pa en riktig
kamerabild med ett ROI.

Exempel:
    python tools/debug_grid.py --synthetic 1111
    python tools/debug_grid.py --image captures/probe.jpg --roi 430,1185,590,1240 --digits 4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ReaderConfig  # noqa: E402
from display_reader import Calibration, find_band, preprocess, read_image  # noqa: E402
from segments import decode_cell, render_number  # noqa: E402


def describe_cell(normalized: np.ndarray, box: tuple[int, int, int, int]) -> tuple[str, float, str]:
    x1, y1, x2, y2 = box
    cell = normalized[y1:y2, x1:x2]
    result = decode_cell(cell)
    values = " ".join(f"{k}={v:.2f}" for k, v in sorted(result.segment_values.items()))
    return result.char, result.confidence, values


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--synthetic", help="rendera denna siffertext i stallet for en bild")
    p.add_argument("--image", type=Path)
    p.add_argument("--roi", help="x1,y1,x2,y2 (default: hela bilden)")
    p.add_argument("--digits", type=int, default=4)
    p.add_argument("--upscale", type=float, default=1.0)
    p.add_argument("--out", type=Path, default=Path("captures/debug_grid.png"))
    args = p.parse_args()

    if args.synthetic:
        canvas, _ = render_number(args.synthetic)
        img = cv2.cvtColor(np.clip(canvas * 255, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        roi = (0, 0, img.shape[1], img.shape[0])
        print(f"Syntetisk text: {args.synthetic!r}  bild {img.shape[1]}x{img.shape[0]}")
    elif args.image:
        img = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
        if img is None:
            print(f"FEL: kunde inte lasa {args.image}")
            return 2
        roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else (0, 0, img.shape[1], img.shape[0])
        print(f"Bild: {args.image.name}  {img.shape[1]}x{img.shape[0]}  ROI={roi}")
    else:
        print("FEL: ange --synthetic eller --image")
        return 2

    cfg = ReaderConfig(digit_count=args.digits, upscale=args.upscale)
    cal = Calibration(roi=roi, digit_count=args.digits)

    normalized, binary = preprocess(img, roi, cfg)
    band = find_band(binary)
    print(f"\nBand (sifferomrade i ROI-koordinater): {band}")
    print(f"Bandets storlek: {band[2] - band[0]}x{band[3] - band[1]} px")

    # Rita en oversikt over band och celler.
    vis = (normalized * 255).astype(np.uint8)
    vis = cv2.cvtColor(vis, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(vis, (band[0], band[1]), (band[2], band[3]), (0, 0, 255), 2)

    reading = read_image(img, cal, cfg, timestamp=0.0)
    print(f"\nLast varde: {reading.value!r}   konfidens: {reading.confidence:.3f}\n")

    for index, box in enumerate(reading.boxes, start=1):
        char, confidence, values = describe_cell(normalized, box)
        print(f"  cell {index}: {box}  ->  {char!r}  konfidens {confidence:.3f}")
        print(f"           {values}")
        cv2.rectangle(vis, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out), vis)
    print(f"\nBild med band och celler -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
