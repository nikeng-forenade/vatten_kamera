"""Mater siffercellerna ur en bildserie och provlaser bilderna.

Verktyget ar den verktygslada som "main.py calibrate" bygger pa: sjalva
matningen ligger i display_reader.measure_cells, och har kan den koras pa en
serie bilder som redan ligger pa disk (sparade med tools/grab.py). Det gor att
kalibreringen kan provas och koras om utan att kameran behover vara igang.

Kor:
    python tools/grab.py --frames 16 --out captures/s2
    python tools/fit_cells.py captures/s2 --read
    python tools/fit_cells.py captures/s2 --read --save
    python tools/fit_cells.py captures/s2 --cells "1019,15,1115,114;..."

Pennan "main.py calibrate --frames 16 --save" gor samma sak direkt mot kameran.
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

from config import ROOT, load_config  # noqa: E402
from display_reader import (  # noqa: E402
    Calibration,
    ReaderError,
    measure_cells,
    preprocess,
    read_image,
)

Box = tuple[int, int, int, int]


def load_frames(directory: Path) -> list[tuple[Path, np.ndarray]]:
    frames: list[tuple[Path, np.ndarray]] = []
    for path in sorted(directory.glob("*.jpg")):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is not None:
            frames.append((path, image))
    return frames


def parse_cells(raw: str) -> list[Box]:
    boxes: list[Box] = []
    for part in raw.split(";"):
        values = [int(float(value)) for value in part.split(",")]
        if len(values) != 4:
            raise SystemExit(f"FEL: {part!r} ar inte x1,y1,x2,y2")
        boxes.append((values[0], values[1], values[2], values[3]))
    return boxes


def load_prior(cfg) -> list[Box] | None:
    """Sifferpositionerna fran forra kalibreringen (x-led ar palitligt darifran)."""
    if not cfg.calibration_file.exists():
        return None
    try:
        data = json.loads(cfg.calibration_file.read_text(encoding="utf-8"))
        return [tuple(box) for box in data["cell_boxes"]]
    except (KeyError, ValueError):
        return None


def save_windows(
    frames: list[tuple[Path, np.ndarray]],
    roi: Box,
    cells: list[Box],
    reader,
    name: str,
) -> Path:
    """Ritar cellrutor och matfonster ovanpa tidsstacken - sa syns det direkt om de sitter ratt."""
    from segments import SEGMENT_BOXES, segment_values

    stacked = np.max(np.stack([image for _path, image in frames]), axis=0)
    normalized, _binary = preprocess(stacked, roi, dataclasses.replace(reader, reference_file=""))
    scale = reader.upscale if reader.upscale else 1.0

    canvas = cv2.cvtColor((normalized * 255).astype("uint8"), cv2.COLOR_GRAY2BGR)
    for index, (ax1, ay1, ax2, ay2) in enumerate(cells, start=1):
        cx1 = int(round((ax1 - roi[0]) * scale))
        cy1 = int(round((ay1 - roi[1]) * scale))
        cx2 = int(round((ax2 - roi[0]) * scale))
        cy2 = int(round((ay2 - roi[1]) * scale))
        if cx2 <= cx1 or cy2 <= cy1:
            continue
        values = segment_values(normalized[cy1:cy2, cx1:cx2])
        lit = "".join(name for name, value in sorted(values.items()) if value > 0.5) or "-"
        print(f"  position {index}: x {ax1}..{ax2}  y {ay1}..{ay2}   lyser {lit:<8}"
              + "  " + " ".join(f"{key}={values[key]:.2f}" for key in sorted(values)))
        for segment, (fx1, fy1, fx2, fy2) in SEGMENT_BOXES.items():
            sx1 = cx1 + int(round(fx1 * (cx2 - cx1)))
            sx2 = cx1 + max(1, int(round(fx2 * (cx2 - cx1))))
            sy1 = cy1 + int(round(fy1 * (cy2 - cy1)))
            sy2 = cy1 + max(1, int(round(fy2 * (cy2 - cy1))))
            colour = (0, 255, 0) if values.get(segment, 0.0) > 0.5 else (0, 0, 255)
            cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), colour, 1)
        cv2.rectangle(canvas, (cx1, cy1), (cx2, cy2), (255, 255, 0), 1)

    out = ROOT / "captures" / f"cells_{name}.png"
    cv2.imwrite(str(out), canvas)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, help="katalog med bilder fran tools/grab.py")
    parser.add_argument("--roi", help="utsnitt x1,y1,x2,y2 (annars CALIBRATION_ROI ur .env)")
    parser.add_argument("--cells", help="fardiga cellrutor: 'x1,y1,x2,y2;...' (annars mats de fram)")
    parser.add_argument("--read", action="store_true", help="läs alla bilder med rutnatet")
    parser.add_argument("--save", action="store_true", help="spara rutnatet i calibration.json")
    parser.add_argument("--windows", action="store_true", help="spara bild med cellrutor och matfonster")
    args = parser.parse_args()

    cfg = load_config()
    roi = tuple(int(value) for value in args.roi.split(",")) if args.roi else cfg.calibration_roi
    if not roi:
        print("FEL: CALIBRATION_ROI saknas i .env")
        return 2

    frames = load_frames(args.directory)
    if not frames:
        print(f"FEL: inga bilder i {args.directory}")
        return 2

    scale = cfg.reader.upscale if cfg.reader.upscale else 1.0
    read_cfg = dataclasses.replace(cfg.reader, reference_file="")
    print(f"{len(frames)} bilder i {args.directory}   ROI {roi}   skala {scale:g}")

    if args.cells:
        absolute = parse_cells(args.cells)
        print("cellrutor fran --cells")
    else:
        try:
            absolute, report = measure_cells(
                [image for _path, image in frames],
                roi,
                cfg.reader,
                cfg.reader.digit_count,
                prior=load_prior(cfg),
            )
        except ReaderError as exc:
            print(f"FEL: {exc}")
            return 1
        for line in report:
            print(f"  {line}")

    print("\ncellrutor i helbildens koordinater:")
    for index, (x1, y1, x2, y2) in enumerate(absolute, start=1):
        print(f"  position {index}: x {x1:4d}..{x2:4d} ({x2 - x1:3d} px)"
              f"   y {y1:4d}..{y2:4d} ({y2 - y1:3d} px)")

    calibration = Calibration(
        roi=tuple(roi), digit_count=cfg.reader.digit_count, cell_boxes=absolute
    )

    if args.windows:
        print("\ntidsstacken med cellerna:")
        out = save_windows(frames, tuple(roi), absolute, cfg.reader, "auto")
        print(f"matfonster -> {out}   (gron = lyser, rod = slackt)")

    if args.read:
        print("\nlasning per bild:")
        for path, image in frames:
            reading = read_image(image, calibration, read_cfg)
            chars = "".join(digit.char for digit in reading.digits)
            confidence = " ".join(f"{digit.confidence:.2f}" for digit in reading.digits)
            print(f"  {path.name:<20} {chars!r:<9} [{confidence}]  varde {reading.value!r}"
                  f"  konfidens {reading.confidence:.2f}")

    if args.save:
        cfg.calibration_file.write_text(
            json.dumps(
                {
                    "roi": list(roi),
                    "digit_count": cfg.reader.digit_count,
                    "cell_boxes": [list(box) for box in absolute],
                    "notes": "cellrutor matt med tools/fit_cells.py - varje position har sin egen hojd",
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nkalibrering sparad -> {cfg.calibration_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
