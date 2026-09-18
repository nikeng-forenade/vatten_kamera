"""Mater var siffrorna sitter i en bild - som siffror, inte ogonmatt.

Skriver ut kolumnprofilen: vilka kolumner som ar tta och vilka som ar morka.
De breda morka partierna ar mellanrummen mellan siffrorna, sa deras lagen talar
om exakt var varje siffra borjar och slutar.

Kor:
    python tools/measure_display.py captures/runs/.../grupp10_1bilder_1528.jpg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from display_reader import clean_binary, find_band, preprocess  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--roi", help="x1,y1,x2,y2 (annars CALIBRATION_ROI ur .env)")
    args = parser.parse_args()

    cfg = load_config()
    image = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if image is None:
        print(f"FEL: kunde inte lasa {args.image}")
        return 2

    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else cfg.calibration_roi
    if not roi:
        print("FEL: ingen ROI")
        return 2

    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0

    normalized, binary = preprocess(image, roi, cfg.reader)
    binary = clean_binary(binary)
    band = find_band(binary)

    bx1, by1, bx2, by2 = band
    print(f"bild  {image.shape[1]}x{image.shape[0]}   ROI {roi}   skala {scale:g}")
    print(f"band  x {roi[0] + bx1 / scale:.0f}..{roi[0] + bx2 / scale:.0f}"
          f"   y {roi[1] + by1 / scale:.0f}..{roi[1] + by2 / scale:.0f}")

    region = binary[by1:by2, bx1:bx2]
    profile = np.count_nonzero(region, axis=0)

    # Dela upp i tta och morka partier.
    lit = profile > 0
    runs: list[tuple[bool, int, int]] = []
    start = 0
    for index in range(1, len(lit)):
        if lit[index] != lit[start]:
            runs.append((bool(lit[start]), start, index))
            start = index
    runs.append((bool(lit[-1]), start, len(lit)))

    def to_absolute(column: int) -> int:
        return int(round(roi[0] + (bx1 + column) / scale))

    print("\nkolumnprofil (tta partier i fetstil, morka i parentes):")
    for is_lit, start, end in runs:
        width = end - start
        text = f"x {to_absolute(start)}..{to_absolute(end)}  ({width} px)"
        print(f"  {'LJUS' if is_lit else 'mork'}  {text}")

    print("\nmorka partier bredare an 15 px ar mellanrum mellan siffror:")
    for is_lit, start, end in runs:
        if not is_lit and (end - start) > 15:
            print(f"  x {to_absolute(start)}..{to_absolute(end)}  bredd {end - start} px")

    return 0


if __name__ == "__main__":
    sys.exit(main())
