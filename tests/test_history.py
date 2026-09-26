"""Tester för historiken — underlaget för grafen i gränssnittet.

Historiken är en rad per läsning i `history.jsonl`. Den ska tåla att en rad blir
trasig (då hoppas bara den över), den ska gå att fråga "senaste dygnet", och den
ska inte kunna växa utan gräns.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import history


def rad(read_at: str, varde: float) -> dict:
    return {"read_at": read_at, "numeric": varde, "display": f"{varde:.2f}", "ok": True}


def test_skriv_och_las_tillbaka(tmp_path: Path) -> None:
    fil = tmp_path / "history.jsonl"

    history.append(rad("2026-09-21T18:00:00", 0.57), path=fil)
    history.append(rad("2026-09-21T18:05:00", 0.56), path=fil)

    rader = history.read(path=fil)
    assert [row["numeric"] for row in rader] == [0.57, 0.56]


def test_historiken_skapas_nar_den_inte_finns(tmp_path: Path) -> None:
    fil = tmp_path / "ny" / "history.jsonl"

    history.append(rad("2026-09-21T18:00:00", 0.5), path=fil)

    assert fil.exists()
    assert len(history.read(path=fil)) == 1


def test_las_utan_fil_ger_tom_lista(tmp_path: Path) -> None:
    assert history.read(path=tmp_path / "finns-inte.jsonl") == []


def flode_rad(nu: datetime, minuter: float, flode: float) -> dict:
    """En lasning med ett flode, `minuter` bakåt i tiden."""
    tid = (nu - timedelta(minutes=minuter)).isoformat(timespec="seconds")
    return {
        "read_at": tid,
        "numeric": 0.37,
        "ok": True,
        "flow_numeric": flode,
        "flow": f"{flode:.2f}",
    }


def test_flode_larm_nar_flodet_ligger_kvar(tmp_path: Path) -> None:
    """Flode over troskeln varje lasning i en halvtimme = nagot rinner."""
    fil = tmp_path / "history.jsonl"
    nu = datetime.now().replace(microsecond=0)
    history.write(
        [flode_rad(nu, minuter, flode) for minuter, flode in ((30, 0.12), (20, 0.11), (10, 0.13), (0, 0.10))],
        path=fil,
    )

    larm = history.flode_larm(troskel=0.05, minuter=30.0, path=fil, nu=nu)

    assert larm["larm"] is True
    assert larm["antal"] == 4
    assert "0.10" in larm["text"]


def test_inget_flode_larm_nar_pumpen_bara_korde_en_stund(tmp_path: Path) -> None:
    """Ett enstaka flode ar normalt - det ska inte larma."""
    fil = tmp_path / "history.jsonl"
    nu = datetime.now().replace(microsecond=0)
    history.write(
        [flode_rad(nu, minuter, flode) for minuter, flode in ((30, 0.00), (20, 0.00), (10, 0.00), (0, 0.12))],
        path=fil,
    )

    assert history.flode_larm(troskel=0.05, minuter=30.0, path=fil, nu=nu)["larm"] is False


def test_inget_flode_larm_av_en_enda_farsk_lasning(tmp_path: Path) -> None:
    """Tva sparrar: minst tva lasningar, och fonstret ska vara tackt."""
    fil = tmp_path / "history.jsonl"
    nu = datetime.now().replace(microsecond=0)
    history.write([flode_rad(nu, 0, 0.20)], path=fil)
    assert history.flode_larm(troskel=0.05, minuter=30.0, path=fil, nu=nu)["larm"] is False

    # ...och flode over troskeln i bara tio minuter ar for kort.
    history.write([flode_rad(nu, minuter, 0.20) for minuter in (10, 5, 0)], path=fil)
    assert history.flode_larm(troskel=0.05, minuter=30.0, path=fil, nu=nu)["larm"] is False


def test_flode_larm_tal_utan_flode(tmp_path: Path) -> None:
    """Gammal historik utan flode ska inte krascha - eller larma."""
    fil = tmp_path / "history.jsonl"
    nu = datetime.now().replace(microsecond=0)
    history.write(
        [
            {
                "read_at": (nu - timedelta(minutes=minuter)).isoformat(timespec="seconds"),
                "numeric": 0.37,
                "ok": True,
            }
            for minuter in (30, 20, 10, 0)
        ],
        path=fil,
    )

    larm = history.flode_larm(troskel=0.05, minuter=30.0, path=fil, nu=nu)

    assert larm["larm"] is False
    assert larm["flode"] is None


def test_trasig_rad_hoppas_over(tmp_path: Path) -> None:
    fil = tmp_path / "history.jsonl"
    fil.write_text(
        '{"read_at": "2026-09-21T18:00:00", "numeric": 0.5}\n'
        "det har ar inte json\n"
        "\n"
        '{"read_at": "2026-09-21T18:05:00", "numeric": 0.4}\n',
        encoding="utf-8",
    )

    rader = history.read(path=fil)

    assert [row["numeric"] for row in rader] == [0.5, 0.4]


def test_raderna_sorteras_pa_tid(tmp_path: Path) -> None:
    fil = tmp_path / "history.jsonl"
    fil.write_text(
        '{"read_at": "2026-09-21T18:05:00", "numeric": 0.4}\n'
        '{"read_at": "2026-09-21T18:00:00", "numeric": 0.5}\n',
        encoding="utf-8",
    )

    assert [row["numeric"] for row in history.read(path=fil)] == [0.5, 0.4]


def test_bara_senaste_timmarna(tmp_path: Path) -> None:
    fil = tmp_path / "history.jsonl"
    nu = datetime.now()
    history.write(
        [
            rad((nu - timedelta(hours=30)).isoformat(timespec="seconds"), 0.9),
            rad((nu - timedelta(hours=2)).isoformat(timespec="seconds"), 0.5),
            rad((nu - timedelta(minutes=5)).isoformat(timespec="seconds"), 0.4),
        ],
        path=fil,
    )

    rader = history.read(hours=6, path=fil)

    assert [row["numeric"] for row in rader] == [0.5, 0.4]


def test_historiken_kapas(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(history, "MAX_ROWS", 3)
    fil = tmp_path / "history.jsonl"

    for i in range(5):
        history.append(rad(f"2026-09-21T18:0{i}:00", 0.5), path=fil)

    rader = history.read(path=fil)
    assert len(rader) == 3
    # De tre senaste ar kvar.
    assert [row["read_at"] for row in rader] == [
        "2026-09-21T18:02:00",
        "2026-09-21T18:03:00",
        "2026-09-21T18:04:00",
    ]


def test_limit_ger_de_senaste(tmp_path: Path) -> None:
    fil = tmp_path / "history.jsonl"
    history.write([rad(f"2026-09-21T18:0{i}:00", 0.4 + i / 100) for i in range(5)], path=fil)

    rader = history.read(limit=2, path=fil)

    assert [row["read_at"] for row in rader] == ["2026-09-21T18:03:00", "2026-09-21T18:04:00"]
