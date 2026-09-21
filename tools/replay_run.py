"""Spelar upp en sparad korning och visar vad rostningen ger.

Nar en nattlig korning har gatt fel ligger bilderna kvar i `captures/runs/<tid>/`.
Det har verktyget kor samma tolkning och samma rostning pa dem, sa att man kan se
vad displayen visade och vad programmet gjorde av det - utan att vanta till nasta
natt.

Bilderna i en korning ar den mittersta bilden ur varje grupp (se
`_capture_and_read` i pipeline.py). Filnamnet sager hur manga bilder gruppen
vila pa - `grupp20_20bilder_090.jpg` - och den vikten anvands, sa att rostningen
blir den samma som i korningen. Bilderna sjalva ar anda bara en per grupp, sa
konfidensen blir nagot lagre an i korningen (dar tolkningen gjordes pa medianen
av gruppens alla bilder).

Kor:
    python tools/replay_run.py captures/runs/20260920_210122
    python tools/replay_run.py captures/serie

Avslutar med status 0 om ett varde kom ut, annars 1 - sa att den gar att köra
skriptat.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from display_reader import (  # noqa: E402
    Calibration,
    Reading,
    consensus,
    crop_roi,
    group_similar,
    image_from_crop,
    read_image,
    typical_crops,
)
from pipeline import page_kind, voting_targets  # noqa: E402


def group_order(path: Path) -> tuple[int, str]:
    """Sorterar 'grupp07_11bilder_091.jpg' i korningens ordning, inte i bokstavsordning."""
    match = re.match(r"grupp(\d+)", path.name)
    return (int(match.group(1)) if match else 0, path.name)


def frames_behind(path: Path) -> int:
    """Hur manga bilder gruppen vila pa, ur filnamnet fran korningen.

    Bilderna sparas som 'grupp07_11bilder_091.jpg', dar 11 ar antalet bilder i
    gruppen. Utan den siffran skulle en grupp pa elva bilder vaga lika lite som
    en enstaka toning, och rostningen skulle bli en annan an i korningen.
    """
    match = re.search(r"_(\d+)bilder_", path.name)
    return int(match.group(1)) if match else 1


def build_readings(directory: Path, *, threshold: float, cfg, calibration: Calibration) -> list[Reading]:
    """Tolkar bilderna i katalogen precis som korningen gjorde."""
    crops = []
    weights: list[int] = []
    for path in sorted(directory.glob("*.jpg"), key=group_order):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is not None:
            crops.append(crop_roi(image, calibration.roi, cfg.reader.channel))
            weights.append(frames_behind(path))

    if not crops:
        return []

    readings: list[Reading] = []
    for indices in group_similar(crops, threshold):
        typical = typical_crops(crops, indices)
        reading = read_image(
            image_from_crop(typical, calibration.roi),
            calibration,
            cfg.reader,
            timestamp=float(len(indices)),
        )
        # Rosten vager lika tungt som antalet bilder i gruppen, precis som i korningen.
        reading.weight = sum(weights[index] for index in indices)
        readings.append(reading)
    return readings


def print_readings(readings: list[Reading], voted: set[int]) -> None:
    print(f"\n{'grupp':>5}  {'bilder':>6}  {'sida':<7} {'varde':<7} {'konfidens':>9}  rostad")
    for number, reading in enumerate(readings, start=1):
        print(
            f"{number:>5}  {max(1, reading.weight):>6}  {page_kind(reading):<7} "
            f"{reading.value!r:<7} {reading.confidence:>9.2f}  {'ja' if id(reading) in voted else 'nej'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Spela upp en sparad korning")
    parser.add_argument(
        "directory",
        type=Path,
        nargs="+",
        help="katalog med bilder (t.ex. captures/runs/<tid>)",
    )
    parser.add_argument(
        "--min-confidens",
        type=float,
        help="minsta konfidens per siffra (annars MIN_CONFIDENCE ur .env)",
    )
    parser.add_argument(
        "--min-enighet",
        type=int,
        help="minsta antal roster (annars MIN_AGREEMENT ur .env)",
    )
    parser.add_argument("--tyst", action="store_true", help="bara sammanfattningen per katalog")
    args = parser.parse_args()

    cfg = load_config()
    calibration = Calibration.load(cfg.calibration_file)

    failed = 0
    for directory in args.directory:
        if replay(directory, cfg, calibration, quiet=args.tyst, args=args) is False:
            failed += 1

    if len(args.directory) > 1:
        print(f"\n{len(args.directory) - failed} av {len(args.directory)} kataloger gav ett varde")
    return 1 if failed else 0


def replay(directory: Path, cfg, calibration: Calibration, *, quiet: bool, args) -> bool:
    readings = build_readings(
        directory,
        threshold=cfg.run.group_threshold,
        cfg=cfg,
        calibration=calibration,
    )
    if not readings:
        print(f"FEL: inga bilder i {directory}")
        return False

    target, note = voting_targets(readings)
    print(f"\n=== {directory} ===")
    if not quiet:
        print_readings(readings, {id(reading) for reading in target})
    if note:
        print(note)

    result = consensus(
        target,
        min_agreement=args.min_enighet or cfg.run.min_agreement,
        min_confidence=args.min_confidens or cfg.run.min_confidence,
        decimals=cfg.reader.decimals,
    )
    print(
        f"rostar om {len(target)} av {len(readings)} grupper  "
        f"->  varde {result.value!r}  {result.votes} roster  konfidens {result.confidence:.2f}"
    )

    saved = directory / "summary.json"
    if saved.exists():
        was = json.loads(saved.read_text(encoding="utf-8"))
        print(
            f"korningen sjalv:  varde {was.get('value')!r}  "
            f"{was.get('votes')} roster  konfidens {(was.get('confidence') or 0):.2f}"
        )

    if not result.ok:
        print("inget varde kom ut ur rostningen")
        return False

    if cfg.reader.decimals:
        print(f"vardet ar {result.numeric:.{cfg.reader.decimals}f}")
    else:
        print(f"vardet ar {result.numeric:.0f}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
