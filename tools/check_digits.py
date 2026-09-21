"""Mater avlasaren mot riktiga bilder med kanda siffror.

Korningen 2026-09-21 visade vardesidan `0.64`, men en av siffrorna fick
konfidens 0.00 och en annan 0.32. Da blir hela sidan "okant" i rostningen, och
programmet publicerade flodessidan `0.00` i stallet.

Det har verktyget mater varje siffra i sparade bilder dar vi VET vad displayen
visade, och skriver ut vad avlasaren fick ut - sa att matfonstren i segments.py
kan andras med matt i handen i stallet for gissning.

Kor:
    python tools/check_digits.py
    python tools/check_digits.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ROOT, load_config  # noqa: E402
from display_reader import Calibration, read_image  # noqa: E402
from segments import SEGMENT_BOXES  # noqa: E402

# Bilder dar displayen visar ett kant varde. Textens langd = antal celler, och
# en blankstol i borjan ar den slackta forsta positionen pa en vardesida.
#
# 2026-09-21 17:14 (korningen main.py read gjorde):
#   1709 - klockan, 0200 - spolttiden, 064 - vardet, 000 - flodet, 0700 - en tidssida
# 2026-09-20 21:08 / 21:05 / 21:01 / 20:43:
#   2103, 2104, 2100, 2038 - klockan; 0200 - spolttiden; 090, 091, 092 - vardet
#   000 - flodet; 2056, 2057 - klockan
LABELS: list[tuple[str, str]] = [
    ("captures/runs/20260921_171444/grupp01_1bilder_1709.jpg", "1709"),
    ("captures/runs/20260921_171444/grupp19_1bilder_0200.jpg", "0200"),
    ("captures/runs/20260921_171444/grupp20_15bilder_064.jpg", " 064"),
    ("captures/runs/20260921_171444/grupp21_15bilder_000.jpg", " 000"),
    ("captures/runs/20260921_171444/grupp22_14bilder_1710.jpg", "1710"),
    ("captures/runs/20260921_171444/grupp23_1bilder_0700.jpg", "0700"),
    ("captures/runs/20260920_204313/grupp01_1bilder_082.jpg", " 092"),
    ("captures/runs/20260920_204313/grupp03_15bilder_000.jpg", " 000"),
    ("captures/runs/20260920_204313/grupp04_1bilder_2038.jpg", "2038"),
    ("captures/runs/20260920_204313/grupp05_14bilder_2038.jpg", "2038"),
    ("captures/runs/20260920_210122/grupp09_1bilder_2056.jpg", "2056"),
    ("captures/runs/20260920_210122/grupp22_1bilder_0200.jpg", "0200"),
    ("captures/runs/20260920_210122/grupp38_14bilder_091.jpg", " 091"),
    ("captures/runs/20260920_210514/grupp07_11bilder_2100.jpg", "2100"),
    ("captures/runs/20260920_210514/grupp27_15bilder_090.jpg", " 090"),
    ("captures/runs/20260920_210514/grupp31_3bilder_000.jpg", " 000"),
    ("captures/runs/20260920_210824/grupp04_13bilder_2103.jpg", "2103"),
    ("captures/runs/20260920_210824/grupp20_20bilder_090.jpg", " 090"),
    ("captures/runs/20260920_210824/grupp24_7bilder_000.jpg", " 000"),
    ("captures/runs/20260920_210824/grupp25_5bilder_2104.jpg", "2104"),
]

# En siffra under den har konfidensen ar sa osaker att sidan inte kan rostas fram.
WANT_CONFIDENCE = 0.5


def main() -> int:
    parser = argparse.ArgumentParser(description="Mater avlasaren mot kanda bilder")
    parser.add_argument("--verbose", action="store_true", help="visa segmentvardena for felande siffror")
    parser.add_argument("--min", type=float, default=WANT_CONFIDENCE, help="konfidens att klara")
    args = parser.parse_args()

    import cv2

    cfg = load_config()
    calibration = Calibration.load(cfg.calibration_file)

    right = 0
    weak = 0
    wrong = 0
    total = 0

    for relative, expected in LABELS:
        path = ROOT / relative
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            print(f"SAKNAS  {relative}")
            continue

        reading = read_image(image, calibration, cfg.reader)
        got = f"{reading.value:<{len(expected)}}"
        marks = []
        for index, wanted in enumerate(expected):
            total += 1
            char = reading.digits[index].char if index < len(reading.digits) else "?"
            confidence = reading.digits[index].confidence if index < len(reading.digits) else 0.0
            if char != wanted:
                wrong += 1
                marks.append(f"[{index + 1}: {char!r} i stallet for {wanted!r}]")
            elif confidence < args.min:
                weak += 1
                marks.append(f"[{index + 1}: {char} bara {confidence:.2f}]")
            else:
                right += 1

        flag = "  " if not marks else "->"
        print(f"{flag} {relative.split('/')[-1]:<32} visar {expected!r}  lases {got!r}  {' '.join(marks)}")

        if marks and args.verbose:
            for index, wanted in enumerate(expected):
                values = reading.digits[index].segment_values
                print(
                    f"      cell {index + 1} ({wanted!r}): "
                    + " ".join(f"{name}={values.get(name, 0.0):.2f}" for name in SEGMENT_BOXES)
                )

    print()
    print(f"siffror: {total}   ratt {right}   ratt men svag (< {args.min}) {weak}   fel {wrong}")
    return 0 if wrong == 0 and weak == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
