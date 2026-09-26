"""Gör brand-ikonen som Home Assistant och HACS visar för integrationen.

Home Assistant letar i `custom_components/vatten_kamera/brand/` (se
developers.home-assistant.io, "Brand images - brand/"). Ikonen ska vara
kvadratisk och 256x256 px; den ritas därför i fyrfaldig storlek och skalas ner,
så att kanterna blir mjuka.

Kör:
    .venv\\Scripts\\python.exe tools\\make_icon.py

Bilden skrivs till custom_components/vatten_kamera/brand/icon.png och är samma
fil som går till git - kör om skriptet när ikonen ska ändras.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MAL = ROOT / "custom_components" / "vatten_kamera" / "brand" / "icon.png"

SIDA = 256
SKALA = 4  # ritas storre och skalas ner -> mjuka kanter
N = SIDA * SKALA
RUNDNING = 56 * SKALA

# Blå platta (BGR), lite mörkare nedtill så att den inte ser platt ut.
TOPP = (196, 127, 44)
BOTTEN = (127, 62, 18)


def _rundad_fyrkant() -> np.ndarray:
    """Vit mask med rundade horn, i den stora arbetsytan."""
    mask = np.zeros((N, N), np.uint8)
    r = RUNDNING
    cv2.rectangle(mask, (r, 0), (N - r, N), 255, -1)
    cv2.rectangle(mask, (0, r), (N, N - r), 255, -1)
    for cx, cy in ((r, r), (N - r, r), (r, N - r), (N - r, N - r)):
        cv2.circle(mask, (cx, cy), r, 255, -1)
    return mask


def _droppe() -> np.ndarray:
    """Vit mask med en droppe: en spets upptill och en buk nedtill."""
    mask = np.zeros((N, N), np.uint8)
    mitten = N // 2
    spets = int(mitten - 70 * SKALA)
    buk_y = int(mitten + 30 * SKALA)
    buk_r = int(52 * SKALA)
    cv2.circle(mask, (mitten, buk_y), buk_r, 255, -1)
    cv2.fillPoly(
        mask,
        [np.array([(mitten, spets), (mitten - buk_r, buk_y), (mitten + buk_r, buk_y)], np.int32)],
        255,
    )
    return mask


def bygg() -> np.ndarray:
    """Ikonen som RGBA i 256x256."""
    # Bakgrund: toning uppifran och ner.
    lut = np.linspace(0.0, 1.0, N, dtype=np.float32)[:, None]
    bakgrund = np.zeros((N, N, 3), np.uint8)
    for kanal in range(3):
        bakgrund[:, :, kanal] = (TOPP[kanal] * (1 - lut) + BOTTEN[kanal] * lut).astype(np.uint8)

    bild = np.zeros((N, N, 4), np.uint8)
    platta = _rundad_fyrkant() > 0
    bild[platta, :3] = bakgrund[platta]
    bild[platta, 3] = 255

    droppe = _droppe() > 0
    bild[droppe] = (255, 255, 255, 255)

    return cv2.resize(bild, (SIDA, SIDA), interpolation=cv2.INTER_AREA)


def main() -> int:
    bild = bygg()
    MAL.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(MAL), bild):
        print(f"FEL: kunde inte skriva {MAL}")
        return 1
    print(f"ikon: {MAL.relative_to(ROOT)} ({bild.shape[1]}x{bild.shape[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
