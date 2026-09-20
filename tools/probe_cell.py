"""Skriver en siffercell som en teckenkarta - sa syns var segmenten sitter.

Verktyget finns for att kunna lagga matfonstren (SEGMENT_BOXES i segments.py) pa
ratt stalle. Kartan ar cellen uppdelad i ett rutnat dar varje tecken ar
medelvardet av en liten ruta, och axlarna ar markta med andelar av cellen (0.0
= cellens vansterkant, 1.0 = hoger kant). Da gar det att direkt jamfora med
fonstren, som ocksa ar andelar av cellen.

Kor:
    python tools/probe_cell.py captures/s1/000_152050.jpg --cell 2
    python tools/probe_cell.py captures/s1/000_152050.jpg --cell 2 --cols 60
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from display_reader import preprocess  # noqa: E402
from segments import SEGMENT_BOXES, decode_cell  # noqa: E402

RAMP = " .:-=+*#%@"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--roi", help="x1,y1,x2,y2 (annars CALIBRATION_ROI ur .env)")
    parser.add_argument("--cells", help="cellrutor 'x1,y1,x2,y2;...' (annars calibration.json)")
    parser.add_argument("--cell", type=int, default=0, help="visa bara denna cell (0 = alla)")
    parser.add_argument("--cols", type=int, default=44)
    parser.add_argument("--rows", type=int, default=18)
    args = parser.parse_args()

    cfg = load_config()
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else cfg.calibration_roi
    if not roi:
        print("FEL: ingen ROI")
        return 2

    image = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if image is None:
        print(f"FEL: kunde inte lasa {args.image}")
        return 2

    if args.cells:
        boxes = [
            tuple(int(float(v)) for v in part.split(",")) for part in args.cells.split(";")
        ]
    else:
        data = json.loads(cfg.calibration_file.read_text(encoding="utf-8"))
        boxes = [tuple(box) for box in data["cell_boxes"]]

    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    reader = dataclasses.replace(cfg.reader, reference_file="")
    normalized, _binary = preprocess(image, roi, reader)

    print(f"bild {image.shape[1]}x{image.shape[0]}  ROI {roi}  skala {scale:g}"
          f"  troffel {cfg.reader.threshold}  kanal {cfg.reader.channel!r}")
    print(f"tecken: '{RAMP[0]}' = morkast, '{RAMP[-1]}' = ljusast\n")

    for index, (ax1, ay1, ax2, ay2) in enumerate(boxes, start=1):
        if args.cell and index != args.cell:
            continue
        cx1 = int(round((ax1 - roi[0]) * scale))
        cy1 = int(round((ay1 - roi[1]) * scale))
        cx2 = int(round((ax2 - roi[0]) * scale))
        cy2 = int(round((ay2 - roi[1]) * scale))
        cell = normalized[cy1:cy2, cx1:cx2]
        if cell.size == 0:
            print(f"cell {index}: {ax1},{ay1},{ax2},{ay2} ligger utanfor bilden")
            continue

        result = decode_cell(cell)
        print(f"cell {index}: {ax1},{ay1},{ax2},{ay2}  ({ax2 - ax1}x{ay2 - ay1} px)"
              f"  -> {result.char!r} konfidens {result.confidence:.2f}")

        small = cv2.resize(cell, (args.cols, args.rows), interpolation=cv2.INTER_AREA)
        for row in range(args.rows):
            # Cellens hojdandel vid radens mitt.
            fy = (row + 0.5) / args.rows
            line = "".join(
                RAMP[min(len(RAMP) - 1, max(0, int(round(small[row, col] * 9))))]
                for col in range(args.cols)
            )
            print(f"  {fy:4.2f} |{line}|")

        header = "       |" + "".join(
            str(int(((col + 0.5) / args.cols) * 10) % 10) for col in range(args.cols)
        ) + "|"
        print(header)
        print("         x-andel: 0.0 = vansterkant, 1.0 = hogerkant\n")

        values = result.segment_values
        print("  segment     fonster (andel av cellen)                varde")
        for name, (fx1, fy1, fx2, fy2) in SEGMENT_BOXES.items():
            print(f"    {name}   x {fx1:.2f}-{fx2:.2f}  y {fy1:.2f}-{fy2:.2f}"
                  f"          {values.get(name, 0.0):.2f}"
                  f"{'   <- lyser' if values.get(name, 0.0) > 0.5 else ''}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
