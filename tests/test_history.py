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
