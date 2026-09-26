"""Tester for installningarna som gar att andra i webbgranssnittet.

Det viktiga har ar att .env gar att skriva i utan att kommentarerna forsvinner -
filen ska fortfarande ga att lasa och andra sjalv.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from settings_store import (
    BY_KEY,
    RESTART_KEYS,
    Field,
    apply_changes,
    current,
    normalize,
    read_env,
    restart_required,
    write_env,
)

import settings_store

ENV = """# En kommentar overst
RUN_AT=02:05:00

# Kameran
CAMERA_IP=10.0.0.5
CAMERA_PASSWORD=hemligt

HA_TOKEN=tokentoken
UNIT=
"""


def skriv(tmp_path: Path, text: str = ENV) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_laser_env_som_tabell(tmp_path: Path) -> None:
    values = read_env(skriv(tmp_path))

    assert values["RUN_AT"] == "02:05:00"
    assert values["CAMERA_PASSWORD"] == "hemligt"
    assert values["UNIT"] == ""
    assert "# En kommentar overst" not in values


def test_hemligt_varde_lamnas_aldrig_ut(tmp_path: Path) -> None:
    falt = {item["key"]: item for item in current(skriv(tmp_path))}

    assert falt["HA_TOKEN"]["kind"] == "secret"
    assert falt["HA_TOKEN"]["value"] == ""
    assert falt["HA_TOKEN"]["har_varde"] is True
    assert falt["UNIT"]["har_varde"] is False


def test_tomt_hemligt_falt_behaller_det_gamla(tmp_path: Path) -> None:
    path = skriv(tmp_path)
    changed, problems = apply_changes({"HA_TOKEN": ""}, path)

    assert changed == {} and problems == {}
    assert read_env(path)["HA_TOKEN"] == "tokentoken"


def test_skriver_bara_den_andrade_raden(tmp_path: Path) -> None:
    path = skriv(tmp_path)
    changed, problems = apply_changes({"RUN_AT": "2:00"}, path)

    assert problems == {}
    assert changed == {"RUN_AT": "02:00:00"}
    text = path.read_text(encoding="utf-8")
    assert "RUN_AT=02:00:00" in text
    assert "# En kommentar overst" in text
    assert "# Kameran" in text
    assert "CAMERA_IP=10.0.0.5" in text
    assert "CAMERA_PASSWORD=hemligt" in text


def test_ny_nyckel_laggs_sist(tmp_path: Path) -> None:
    path = skriv(tmp_path)
    apply_changes({"STATUS_PORT": "8099"}, path)

    rader = path.read_text(encoding="utf-8").splitlines()
    assert rader[-1] == "STATUS_PORT=8099"


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("RUN_AT", "2:5", "02:05:00"),
        ("RUN_AT", "23:59:59", "23:59:59"),
        ("MIN_AGREEMENT", "3", "3"),
        ("MIN_CONFIDENCE", "0,5", "0.5"),
        ("SAVE_FRAMES", "ja", "true"),
        ("SAVE_FRAMES", "nej", "false"),
        ("COLOR_CHANNEL", "b", "b"),
        ("UNIT", "", ""),
    ],
)
def test_godkanda_varden(key: str, raw: str, expected: str) -> None:
    assert normalize(BY_KEY[key], raw) == expected


@pytest.mark.parametrize(
    "key, raw",
    [
        ("RUN_AT", "25:00"),
        ("RUN_AT", "kvart i tva"),
        ("MIN_AGREEMENT", "manga"),
        ("MIN_CONFIDENCE", "1.5"),
        ("COLOR_CHANNEL", "lila"),
        ("CAMERA_IP", "10.0.0.5\nRM -RF"),
    ],
)
def test_avvisar_oanvandbara_varden(key: str, raw: str) -> None:
    with pytest.raises(ValueError):
        normalize(BY_KEY[key], raw)


def test_okand_nyckel_avvisas(tmp_path: Path) -> None:
    changed, problems = apply_changes({"HEMLIG_FIL": "/etc/passwd"}, skriv(tmp_path))

    assert changed == {}
    assert "okand installning" in problems["HEMLIG_FIL"]


def test_saknad_nyckel_visar_standardvardet(tmp_path: Path) -> None:
    # Utan standardvarde skulle granssnittet visa "av" for en installning som i
    # sjalva verket ar pa - och skriva ner det nar nagon trycker Spara.
    falt = {item["key"]: item for item in current(skriv(tmp_path))}

    assert falt["STATUS_ALLOW_RUN"]["value"] == "true"
    assert falt["STATUS_PORT"]["value"] == "8099"


def test_spara_utan_andringar_ror_inte_filen(tmp_path: Path) -> None:
    path = skriv(tmp_path)
    innan = path.read_text(encoding="utf-8")

    # Det granssnittet skickar nar inget har andrats (tomt), och nar ett falt
    # skickas tillbaka med samma varde som det visade.
    changed, problems = apply_changes({}, path)
    assert changed == {} and problems == {}

    changed, problems = apply_changes({"RUN_AT": "02:05:00"}, path)
    assert changed == {} and problems == {}

    assert path.read_text(encoding="utf-8") == innan


def test_skriv_env_ror_inte_andra_rader(tmp_path: Path) -> None:
    path = skriv(tmp_path)
    write_env(read_env(path), {"CAMERA_IP": "10.0.0.5"}, path)

    text = path.read_text(encoding="utf-8")
    assert "CAMERA_IP=10.0.0.5" in text
    assert "RUN_AT=02:05:00" in text
    assert text.count("CAMERA_IP=") == 1


def test_alla_falt_har_unik_nyckel() -> None:
    # FIELDS, inte BY_KEY: en dubblett forsvinner tyst i en dict och blev tva
    # likadana rutor i granssnittet (bara den forsta gick att andra).
    nycklar = [item.key for item in settings_store.FIELDS]
    assert len(nycklar) == len(set(nycklar)), f"dubbletter: {sorted(set(nycklar))}"
    assert all(isinstance(item, Field) for item in BY_KEY.values())


def test_bara_granssnittets_egna_falt_kraver_omstart() -> None:
    """Allt annat slar igenom vid nasta lasning - tjansten laser om .env sjalv."""
    vanliga = restart_required({"THRESHOLD": "240", "EVERY_MINUTES": "10", "MODE": "manuell"})
    assert vanliga == []

    krangliga = restart_required({"STATUS_PORT": "8099", "THRESHOLD": "240"})
    assert krangliga == ["STATUS_PORT"]


def test_omstart_nycklarna_finns_bland_falten() -> None:
    """En nyckel som inte ar ett falt kan heller inte andras - da ar listan fel."""
    for key in RESTART_KEYS:
        assert key in BY_KEY, f"{key} finns inte bland installningarna"


def test_omstart_nycklarna_ar_granssnittets_egna() -> None:
    # Bara granssnittets egen port, adress och av/pa-knappar kraver en omstart.
    assert RESTART_KEYS == {
        "STATUS_PORT",
        "STATUS_BIND",
        "STATUS_LIVE",
        "STATUS_ALLOW_RUN",
        "STATUS_ALLOW_RESTART",
    }
