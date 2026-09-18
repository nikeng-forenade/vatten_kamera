"""Tester for inriktningen mot referensbilden.

Inriktningen ska gora lasningen okanslig for att kameran rubbas nagra pixel.
Testet ritar en enkel display, flyttar den en kand stracka och kontrollerar att
forskjutningen hittas med ratt tecken: cellerna ska flyttas lika mycket som
bilden, annars hamnar matfonstren annu langre fel an utan inriktning.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from display_reader import estimate_shift, load_reference, save_reference


def _display() -> np.ndarray:
    """En bild med tre sifferliknande figurer, hog kontrast mot svart."""
    image = np.zeros((140, 400), dtype=np.float32)
    for x in (20, 130, 240):
        image[24:116, x : x + 80] = 1.0
        image[58:84, x + 22 : x + 58] = 0.0  # hal i mitten, som i en nolla
    return image


def _shift(image: np.ndarray, dx: float, dy: float) -> np.ndarray:
    matrix = np.float32([[1.0, 0.0, dx], [0.0, 1.0, dy]])
    return cv2.warpAffine(image, matrix, (image.shape[1], image.shape[0]))


def test_hittar_forskjutningen_med_ratt_tecken() -> None:
    reference = _display()
    current = _shift(reference, 7, -5)

    dx, dy, response = estimate_shift(reference, current)

    assert dx == pytest.approx(7.0, abs=1.0)
    assert dy == pytest.approx(-5.0, abs=1.0)
    assert response > 0.05


def test_orord_bild_ger_ingen_forskjutning() -> None:
    reference = _display()

    dx, dy, _response = estimate_shift(reference, reference)

    assert abs(dx) < 0.5
    assert abs(dy) < 0.5


def test_olika_storlek_ger_noll() -> None:
    dx, dy, response = estimate_shift(_display(), _display()[:70, :200])

    assert (dx, dy, response) == (0.0, 0.0, 0.0)


def test_referensen_sparas_och_lases(tmp_path) -> None:
    path = tmp_path / "reference.png"
    image = _display()

    save_reference(image, str(path))
    loaded = load_reference(str(path))

    assert loaded is not None
    assert loaded.shape == image.shape
    # PNG:n ar 8-bitars, sa ett halvt stegs avrundningsfel ar vanted.
    assert np.allclose(loaded, image, atol=1.0 / 255.0 + 1e-6)


def test_saknad_referens_ger_none(tmp_path) -> None:
    assert load_reference("") is None
    assert load_reference(str(tmp_path / "finns-inte.png")) is None
