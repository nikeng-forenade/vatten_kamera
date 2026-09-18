"""Verktyg for att hitta och verifiera ROI (display-utsnittet) i en kamerabild.

Exempel:
    # Beskar en region och forstorar den 8x
    python tools/roi_tool.py captures/bild.jpg --x1 380 --y1 1150 --x2 660 --y2 1290 --scale 8

    # Ritar ROI-ramen pa hela bilden sa du ser att den sitter ratt
    python tools/roi_tool.py captures/bild.jpg --x1 380 --y1 1150 --x2 660 --y2 1290 --draw

    # Ritar ett koordinatrutnat over hela bilden (hjalp att hitta siffrorna)
    python tools/roi_tool.py captures/bild.jpg --grid 100
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ROI-verktyg for vatten_kamera")
    p.add_argument("image", type=Path, help="sokvag till bilden")
    p.add_argument("--x1", type=int)
    p.add_argument("--y1", type=int)
    p.add_argument("--x2", type=int)
    p.add_argument("--y2", type=int)
    p.add_argument("--scale", type=float, default=8.0, help="forstoring av utsnittet")
    p.add_argument("--draw", action="store_true", help="rita ROI-ramen pa hela bilden")
    p.add_argument("--grid", type=int, help="rita rutnat med denna steglangd (px)")
    p.add_argument("--out", type=Path, help="utfil (default: <bild>_roi.png)")
    p.add_argument("--binarize", action="store_true", help="visa trosklad svartvit version av utsnittet")
    return p.parse_args()


def draw_grid(img: np.ndarray, step: int) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    for x in range(0, w, step):
        color = (0, 0, 255) if x % (step * 5) == 0 else (0, 200, 0)
        cv2.line(out, (x, 0), (x, h), color, 1)
        if x % (step * 5) == 0:
            cv2.putText(out, str(x), (x + 4, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    for y in range(0, h, step):
        color = (0, 0, 255) if y % (step * 5) == 0 else (0, 200, 0)
        cv2.line(out, (0, y), (w, y), color, 1)
        if y % (step * 5) == 0:
            cv2.putText(out, str(y), (6, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return out


def main() -> int:
    args = parse_args()
    img = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if img is None:
        print(f"FEL: kunde inte lasa {args.image}")
        return 2

    h, w = img.shape[:2]
    print(f"Bild: {args.image.name}  {w}x{h}px")

    out_path = args.out or args.image.with_name(args.image.stem + "_roi.png")

    if args.grid:
        out = draw_grid(img, args.grid)
        cv2.imwrite(str(out_path), out)
        print(f"Rutnat ({args.grid}px) -> {out_path}")
        return 0

    if args.draw:
        if None in (args.x1, args.y1, args.x2, args.y2):
            print("FEL: --draw kraver --x1 --y1 --x2 --y2")
            return 2
        out = img.copy()
        cv2.rectangle(out, (args.x1, args.y1), (args.x2, args.y2), (0, 0, 255), 4)
        cv2.imwrite(str(out_path), out)
        print(f"ROI-ram ritad -> {out_path}")
        return 0

    if None in (args.x1, args.y1, args.x2, args.y2):
        print("FEL: ange --x1 --y1 --x2 --y2 (eller --grid/--draw)")
        return 2

    x1, y1 = max(0, args.x1), max(0, args.y1)
    x2, y2 = min(w, args.x2), min(h, args.y2)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        print("FEL: tomt utsnitt, kontrollera koordinaterna")
        return 2

    big = cv2.resize(
        crop,
        None,
        fx=args.scale,
        fy=args.scale,
        interpolation=cv2.INTER_CUBIC,
    )
    cv2.imwrite(str(out_path), big)
    print(f"Utsnitt {x2 - x1}x{y2 - y1}px forstort {args.scale}x -> {out_path}")

    if args.binarize:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th_big = cv2.resize(th, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_NEAREST)
        bin_path = out_path.with_name(out_path.stem + "_bin.png")
        cv2.imwrite(str(bin_path), th_big)
        print(f"Trosklad version -> {bin_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
