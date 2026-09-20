"""Tester for den nattliga korningen.

Det som provas har ar vilka lasningar som far ligga till grund for rostningen:
vardet som ska ut visas strax efter att pumpen slar om till spolning, och resten
av fonstret visar displayen sina andra sidor.

Kor:  .venv/Scripts/python.exe -m pytest tests -q
"""

from __future__ import annotations

from pipeline import page_kind, readings_after_recharge
from segments import DecodeResult


def cell(char: str) -> DecodeResult:
    return DecodeResult(
        char=char, confidence=0.9 if char != " " else 0.9, error=0.0, second_error=1.0, segment_values={}
    )


def reading(value: str, *, digits: list[str], confidence: float) -> object:
    from display_reader import Reading

    return Reading(
        value=value,
        confidence=confidence,
        digits=[cell(char) for char in digits],
        timestamp=0.0,
        boxes=[],
    )


def spoltid() -> object:
    """Displayens 02:00-sida: alla fyra positioner tands."""
    return reading("0200", digits=list("0200"), confidence=0.0)


def varde(text: str, confidence: float = 0.7) -> object:
    """En vardesida: vardet i position 2-4, den forsta slackt."""
    return reading(text, digits=[" ", *text], confidence=confidence)


def annan_tidssida(text: str) -> object:
    return reading(text, digits=list(text), confidence=0.0)


def test_page_kind_skiljer_tidssida_fran_vardesida() -> None:
    assert page_kind(spoltid()) == "tid"
    assert page_kind(annan_tidssida("1832")) == "tid"
    assert page_kind(varde("116")) == "varde"


def test_valjer_vardet_som_kommer_efter_spolttidssidan() -> None:
    readings = [
        varde("037"),  # vardesida som visas innan spolningen
        spoltid(),
        varde("116"),  # vardet efter 02:00 - det som ska ut
        annan_tidssida("0800"),
    ]
    chosen = readings_after_recharge(readings)

    assert [item.value for item in chosen] == ["116"]


def test_hoppar_over_en_tidssida_mellan_spolttid_och_varde() -> None:
    readings = [spoltid(), annan_tidssida("0800"), varde("116")]
    assert [item.value for item in readings_after_recharge(readings)] == ["116"]


def test_utan_spolttidssida_rostas_inget_bort() -> None:
    # Sa lange displayen inte visat 02:00 far hela korningen ligga till grund.
    readings = [varde("037"), varde("116")]
    assert readings_after_recharge(readings) == []


def test_tva_omgangar_ger_tva_roster_for_samma_varde() -> None:
    # Displayen vaxlar i cykler: spolttid -> varde -> annan sida -> spolttid ...
    readings = [
        spoltid(),
        varde("116"),
        annan_tidssida("0800"),
        spoltid(),
        varde("116"),
        varde("037"),
    ]
    chosen = readings_after_recharge(readings)

    assert [item.value for item in chosen] == ["116", "116"]


def test_samma_vardesida_raknas_bara_en_gang() -> None:
    # Flera spolttidssidor i rad pekar pa samma vardesida. Utan det skyddet
    # skulle en enda grupp bilder kunna vaga hur tungt som helst.
    readings = [spoltid(), spoltid(), spoltid(), varde("116")]
    chosen = readings_after_recharge(readings)

    assert [item.value for item in chosen] == ["116"]
