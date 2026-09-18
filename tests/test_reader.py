"""Tester for avlasningen.

Testerna kor mot syntetiskt ritade sjusegmentsiffror, sa att logiken kan
verifieras utan att kameran ar uppkopplad eller ratt placerad.

Kor:  .venv/Scripts/python.exe -m pytest tests -q
"""

from __future__ import annotations

import cv2
import numpy as np

from config import ReaderConfig
from display_reader import Calibration, Reading, consensus, read_image
from segments import DIGIT_MASKS, decode_cell, render_digit, render_number


def to_bgr(canvas: np.ndarray) -> np.ndarray:
    """Gor om en float-bild (0..1) till en vanlig BGR-bild."""
    img = np.clip(canvas * 255.0, 0, 255).astype(np.uint8)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def test_alla_siffror_rena() -> None:
    for digit in "0123456789":
        result = decode_cell(render_digit(digit))
        assert result.char == digit, f"{digit} lastes som {result.char}"
        assert result.confidence > 0.6, f"{digit} hade lag konfidens {result.confidence}"


def test_alla_siffror_suddiga_och_brusiga() -> None:
    rng = np.random.default_rng(7)
    for digit in "0123456789":
        cell = render_digit(digit, blur=1.3, noise=0.05, rng=rng)
        result = decode_cell(cell)
        assert result.char == digit, f"{digit} blev {result.char} med sudd/brus"


def test_tom_cell_ar_slackt() -> None:
    result = decode_cell(render_digit(" "))
    assert result.blank, f"tom cell blev {result.char!r}"


def test_jamn_gra_yta_ger_inget_varde() -> None:
    # En helt jamn yta (pumphuset) far inte lasas som en atta. Utan kontrast
    # finns ingen siffra, och da ska ingen konfidens rapporteras.
    cell = np.full((110, 60), 0.45, dtype=np.float32)
    result = decode_cell(cell)
    assert result.char == "?", f"jamn yta blev {result.char!r}"
    assert result.confidence == 0.0


def test_jamn_yta_ger_ingen_saker_lasning() -> None:
    uniform = np.full((160, 60), int(0.45 * 255), dtype=np.uint8)
    img = cv2.cvtColor(uniform, cv2.COLOR_GRAY2BGR)
    height, width = img.shape[:2]

    cal = Calibration(roi=(0, 0, width, height), digit_count=4)
    cfg = ReaderConfig(digit_count=4, upscale=1.0)

    reading = read_image(img, cal, cfg)
    assert reading.confidence == 0.0, f"jamn yta gav konfidens {reading.confidence}"


def test_alla_siffror_har_unikt_monster() -> None:
    # Skyddar mot att ett tecken rakar fa samma bitmask som ett annat.
    assert len(set(DIGIT_MASKS.values())) == len(DIGIT_MASKS)


def test_las_syntetiskt_tal() -> None:
    canvas, _ = render_number("1050")
    img = to_bgr(canvas)
    height, width = img.shape[:2]

    cal = Calibration(roi=(0, 0, width, height), digit_count=4)
    cfg = ReaderConfig(digit_count=4, upscale=1.0)

    reading = read_image(img, cal, cfg)
    assert reading.value == "1050", f"lastes som {reading.value!r}"
    assert reading.numeric == 1050.0
    assert reading.confidence > 0.5


def test_las_tal_med_etta_forst_och_sist() -> None:
    # En etta lyser bara i hoger halva av cellen - rutnatet maste anda hamna ratt.
    for text in ("1000", "1111", "2001", "1250"):
        canvas, _ = render_number(text)
        img = to_bgr(canvas)
        height, width = img.shape[:2]

        cal = Calibration(roi=(0, 0, width, height), digit_count=4)
        cfg = ReaderConfig(digit_count=4, upscale=1.0)

        reading = read_image(img, cal, cfg)
        assert reading.value == text, f"{text} lastes som {reading.value!r}"


def test_las_tal_med_svagt_ljus() -> None:
    # Svag belysning: siffrorna ar knappt synliga men ska anda gå att lasa.
    canvas, _ = render_number("0875")
    img = to_bgr(canvas * 0.16 + 0.02)
    height, width = img.shape[:2]

    cal = Calibration(roi=(0, 0, width, height), digit_count=4)
    cfg = ReaderConfig(digit_count=4, upscale=1.0)

    reading = read_image(img, cal, cfg)
    assert reading.value == "0875", f"lastes som {reading.value!r}"


def test_kalibrerade_celler_anvands() -> None:
    canvas, boxes = render_number("1050")
    img = to_bgr(canvas)
    height, width = img.shape[:2]

    cal = Calibration(roi=(0, 0, width, height), digit_count=4, cell_boxes=boxes)
    cfg = ReaderConfig(digit_count=4, upscale=1.0)

    reading = read_image(img, cal, cfg)
    assert reading.value == "1050"


def test_bakgrund_som_ror_vid_kanten_ignoreras() -> None:
    # Pumphuset ar ljust och ror vid ROI:ts kanter - det ska inte tolkas som siffror.
    canvas, _ = render_number("1050")
    height, width = canvas.shape
    framed = np.zeros((height + 40, width + 40), dtype=np.float32)
    framed[20 : 20 + height, 20 : 20 + width] = canvas
    framed[:6, :] = 0.75
    framed[-6:, :] = 0.75
    framed[:, :6] = 0.75
    framed[:, -6:] = 0.75

    img = to_bgr(framed)
    fh, fw = img.shape[:2]
    cal = Calibration(roi=(0, 0, fw, fh), digit_count=4)
    cfg = ReaderConfig(digit_count=4, upscale=1.0)

    reading = read_image(img, cal, cfg)
    assert reading.value == "1050", f"lastes som {reading.value!r}"


def _reading(value: str, confidence: float) -> Reading:
    return Reading(value=value, confidence=confidence, digits=[], timestamp=0.0, boxes=[])


def test_majoritetsrostning_valjer_vanligaste_vardet() -> None:
    readings = [
        _reading("1050", 0.95),
        _reading("1050", 0.90),
        _reading("1050", 0.88),
        _reading("1058", 0.80),
        _reading("1080", 0.78),
    ]
    result = consensus(readings, min_agreement=3, min_confidence=0.75)
    assert result.ok
    assert result.value == "1050"
    assert result.votes == 3
    assert result.numeric == 1050.0


def test_majoritetsrostning_kraver_tillrackligt_med_roster() -> None:
    readings = [_reading("1050", 0.95), _reading("1058", 0.90)]
    result = consensus(readings, min_agreement=3, min_confidence=0.75)
    assert not result.ok
    assert result.value is None


def test_majoritetsrostning_ratar_bort_lag_konfidens() -> None:
    readings = [
        _reading("1050", 0.30),
        _reading("1050", 0.20),
        _reading("1050", 0.95),
    ]
    result = consensus(readings, min_agreement=3, min_confidence=0.75)
    assert not result.ok, "lasningar med lag konfidens ska inte kunna rostas fram"
