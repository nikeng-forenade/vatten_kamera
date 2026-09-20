"""Listar alla ljusa klumpar i ROI:t med absoluta bildkoordinater.

Svarar pa fragan "var sitter siffrorna egentligen?" utan ogonmatt: varje
ihopkopplad ljus flack skrivs ut med sin ruta i helbildens koordinater, sa att
cellrutnatet kan jamforas med verkligheten.

Kor:
    python tools/glyphs.py captures/last_snapshot.jpg
    python tools/glyphs.py captures/last_snapshot.jpg 1010,15,1620,180
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from display_reader import clean_binary, preprocess  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--roi", help="x1,y1,x2,y2 (annars CALIBRATION_ROI ur .env)")
    parser.add_argument("--min-area", type=float, default=4.0)
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

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)

    print(f"bild {image.shape[1]}x{image.shape[0]}  ROI {roi}  skala {scale:g}")
    print(f"troffel {cfg.reader.threshold}  kanal {cfg.reader.channel!r}  celler {cfg.reader.digit_count}")

    rows: list[tuple[float, float, float, float, float]] = []
    for label in range(1, count):
        x, y, width, height, area = stats[label]
        if area < args.min_area:
            continue
        x1 = roi[0] + x / scale
        x2 = roi[0] + (x + width) / scale
        y1 = roi[1] + y / scale
        y2 = roi[1] + (y + height) / scale
        rows.append((x1, y1, x2, y2, float(area)))

    rows.sort(key=lambda r: r[0])
    print(f"\n{len(rows)} klumpar (absoluta koordinater):")
    for index, (x1, y1, x2, y2, area) in enumerate(rows, start=1):
        print(
            f"  {index:>2}  x {x1:7.1f}..{x2:7.1f}  ({x2 - x1:5.1f} px)"
            f"   y {y1:6.1f}..{y2:6.1f}  ({y2 - y1:5.1f} px)   area {area:.0f}"
        )

    if rows:
        bx1 = min(r[0] for r in rows)
        bx2 = max(r[2] for r in rows)
        by1 = min(r[1] for r in rows)
        by2 = max(r[3] for r in rows)
        print(f"\nhela bandet: x {bx1:.0f}..{bx2:.0f} ({(bx2 - bx1):.0f} px)"
              f"  y {by1:.0f}..{by2:.0f} ({(by2 - by1):.0f} px)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
