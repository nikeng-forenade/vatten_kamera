"""Bygger en montage-bild av ROI-utsnittet fran manga bilder - en rad per bild.

Brakar for att snabbt se hur en display vaxlar mellan olika varden.

Exempel:
    python tools/montage_roi.py captures/series/20260918_135913 --roi 440,1200,580,1255 --scale 6
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("folder", type=Path, help="mapp med bilder")
    p.add_argument("--roi", required=True, help="x1,y1,x2,y2")
    p.add_argument("--scale", type=float, default=6.0)
    p.add_argument("--out", type=Path, help="utfil (default: mappen/_montage.png)")
    p.add_argument("--label", action="store_true", help="rita filnamn till vanster")
    args = p.parse_args()

    x1, y1, x2, y2 = (int(v) for v in args.roi.split(","))
    files = sorted(
        f for f in args.folder.iterdir() if f.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not files:
        print(f"Inga bilder i {args.folder}")
        return 2

    rows = []
    for f in files:
        img = cv2.imread(str(f), cv2.IMREAD_COLOR)
        if img is None:
            continue
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        big = cv2.resize(crop, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_CUBIC)
        if args.label:
            pad = np.zeros((big.shape[0], 260, 3), dtype=np.uint8)
            cv2.putText(
                pad,
                f.stem[:12],
                (8, big.shape[0] // 2 + 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (255, 255, 255),
                2,
            )
            big = np.hstack([pad, big])
        rows.append(big)
        rows.append(np.full((6, big.shape[1], 3), 90, dtype=np.uint8))

    if not rows:
        print("Kunde inte bygga montage")
        return 2

    width = max(r.shape[1] for r in rows)
    rows = [r if r.shape[1] == width else cv2.copyMakeBorder(r, 0, 0, 0, width - r.shape[1], cv2.BORDER_CONSTANT) for r in rows]
    montage = np.vstack(rows)

    out = args.out or (args.folder / "_montage.png")
    cv2.imwrite(str(out), montage)
    print(f"{len(files)} bilder -> {out}  ({montage.shape[1]}x{montage.shape[0]}px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
