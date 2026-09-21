"""Tester for vakten: att korningen vet nar vardet ar fangat.

Displayen visar sina sidor i samma ordning hela tiden:

    klockan  ->  spolttiden 02:00  ->  VARDET  ->  flodet  ->  klockan ...

Vardet vi vill ha ar sidan som kommer direkt efter 02:00. Korningen ska darfor
titta tills den sett 02:00, och sedan vardesidan efter den nagra bilder i rad -
och da sluta, i stallet for att sta kvar resten av fonstret.
"""

from __future__ import annotations

from pipeline import PAGE_MIN_FRAMES, READY_FRAMES, ReadyTracker, voting_targets
from tests.test_pipeline import annan_tidssida, spoltid, varde


def test_vardet_ar_fangat_forst_nar_hela_sekvensen_synts() -> None:
    ready = ReadyTracker(frames=2)

    assert ready.feed(varde("091")) is False  # vardesida, men ingen 02:00 fore den
    assert ready.feed(spoltid()) is False  # nu borjar varvet
    assert ready.feed(varde("091")) is False  # en bild pa vardesidan
    assert ready.feed(varde("091")) is True  # tva i rad -> klart


def test_ett_annat_varde_borjar_om_rakningen() -> None:
    ready = ReadyTracker(frames=2)

    ready.feed(spoltid())
    assert ready.feed(varde("091")) is False
    assert ready.feed(varde("000")) is False  # annat varde: ny sida, ny rakning
    assert ready.feed(varde("000")) is True


def test_ny_spolttid_borjar_om() -> None:
    ready = ReadyTracker(frames=2)

    ready.feed(spoltid())
    ready.feed(varde("091"))
    assert ready.feed(spoltid()) is False  # nasta varv
    assert ready.feed(varde("091")) is False
    assert ready.feed(varde("091")) is True


def test_toning_och_tidssida_raknas_inte() -> None:
    ready = ReadyTracker(frames=2)

    ready.feed(spoltid())
    assert ready.feed(annan_tidssida("1745")) is False
    assert ready.feed(varde("091", confidence=0.0)) is False  # olasbar
    assert ready.feed(varde("091")) is False
    assert ready.feed(varde("091")) is True


def test_standardvardet_ar_nagra_bilder() -> None:
    # Vardesidan star stilla i 10-12 s, och bilderna tas med nagra sekunders
    # mellanrum - nanstans mellan tre och sex bilder ar rimligt.
    assert 3 <= READY_FRAMES <= 6


def test_rostningen_anvander_samma_sida_som_vakten_stannar_pa() -> None:
    # Vakten stannar sa snart vardesidan synts READY_FRAMES bilder i rad, sa
    # flodet hinner bara bli ett par bilder. Rostningen ska anda ta vardet - inte
    # flodet som kommer efterat.
    readings = [
        spoltid(bilder=6),
        varde("091", bilder=READY_FRAMES),
        varde("000", bilder=2),
    ]

    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["091"]
    assert note == ""


def test_vakten_ser_till_att_vardesidan_blir_en_sida() -> None:
    """Vakten slutar forst nar vardesidan vilar pa READY_FRAMES bilder, och en

    sida far bestamma vardet forst vid PAGE_MIN_FRAMES. Ar READY_FRAMES mindre
    an PAGE_MIN_FRAMES vore vardesidan for liten nar lasningen slutar - och da
    kunde flodet, som vilar pa fler bilder, bli det som publiceras.
    """
    assert READY_FRAMES >= PAGE_MIN_FRAMES
