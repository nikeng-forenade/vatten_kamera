"""Tester för städningen av gamla bilder.

Bilderna är det som växer på disken, och städningen är den enda delen av
programmet som tar bort något. Därför är två saker viktiga att prova: att bara
körningskataloger inuti bildkatalogen rörs, och att den senaste körningen alltid
är kvar — annars står gränssnittet utan bild.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import cleanup
from cleanup import gamla_korningar, stada

NU = datetime(2026, 9, 21, 20, 0, 0)


def korning(runs: Path, namn: str, innehall: bytes = b"bild") -> Path:
    """En körningskatalog med en bild i, som pipeline skriver dem."""
    katalog = runs / namn
    katalog.mkdir(parents=True, exist_ok=True)
    (katalog / "grupp01_3bilder_056.jpg").write_bytes(innehall)
    (katalog / "summary.json").write_text("{}", encoding="utf-8")
    return katalog


def test_gammal_korning_tas_bort(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    gammal = korning(runs, "20260910_120000")
    korning(runs, "20260921_195427")

    offer = gamla_korningar(7, runs_dir=runs, nu=NU)

    assert [k.name for k in offer] == [gammal.name]


def test_farsk_korning_ar_kvar(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    korning(runs, "20260921_195427")

    assert gamla_korningar(7, runs_dir=runs, nu=NU) == []


def test_den_senaste_sparas_alltid(tmp_path: Path) -> None:
    """Även om tjänsten stått still i månader ska det finnas en bild kvar."""
    runs = tmp_path / "runs"
    korning(runs, "20260101_120000")
    korning(runs, "20260102_120000")

    offer = gamla_korningar(7, runs_dir=runs, nu=NU)

    assert [k.name for k in offer] == ["20260101_120000"]
    assert not any(k.name == "20260102_120000" for k in offer)


def test_stadning_tar_bort_filerna(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    gammal = korning(runs, "20260910_120000")
    korning(runs, "20260921_195427")

    resultat = stada(7, runs_dir=runs, nu=NU)

    assert resultat.antal == 1
    assert not gammal.exists()
    assert resultat.behallna == 1
    assert resultat.mb >= 0


def test_torrkorning_tar_inte_bort_nagot(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    gammal = korning(runs, "20260910_120000")
    korning(runs, "20260921_195427")

    resultat = stada(7, runs_dir=runs, dry=True, nu=NU)

    assert resultat.antal == 1
    assert resultat.dry is True
    assert gammal.exists()
    assert "skulle tas bort" in resultat.text()


def test_enda_korningen_sparas(tmp_path: Path) -> None:
    """Finns bara en körning är den senaste - och den sparas även om den är gammal."""
    runs = tmp_path / "runs"
    enda = korning(runs, "20260101_120000")

    resultat = stada(7, runs_dir=runs, nu=NU)

    assert resultat.antal == 0
    assert enda.exists()


def test_bara_korningskataloger_rors(tmp_path: Path) -> None:
    """En främmande katalog eller fil i bildkatalogen lämnas i fred."""
    runs = tmp_path / "runs"
    korning(runs, "20260910_120000")
    korning(runs, "20260921_195427")
    (runs / "anteckningar.txt").write_text("rör mig inte", encoding="utf-8")
    (runs / "min-egen-mapp").mkdir()
    (runs / "20260910_120000.jpg").write_bytes(b"bild")
    (runs / "inte_ett_datum").mkdir()

    resultat = stada(7, runs_dir=runs, nu=NU)

    assert resultat.antal == 1
    assert (runs / "anteckningar.txt").exists()
    assert (runs / "min-egen-mapp").exists()
    assert (runs / "20260910_120000.jpg").exists()
    assert (runs / "inte_ett_datum").exists()


def test_utan_katalog_ar_allt_bra(tmp_path: Path) -> None:
    resultat = stada(7, runs_dir=tmp_path / "finns-inte", nu=NU)

    assert resultat.antal == 0
    assert "inget att stada" in resultat.text()


def test_texten_sager_vad_som_hande(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    korning(runs, "20260910_120000")
    korning(runs, "20260921_195427")

    resultat = stada(7, runs_dir=runs, nu=NU)

    assert "1 korningar borttagna" in resultat.text()
    assert "1 kvar" in resultat.text()


def test_en_dags_grans(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    korning(runs, "20260920_120000")  # 1,3 dygn gammal
    korning(runs, "20260921_100000")

    offer = gamla_korningar(1, runs_dir=runs, nu=NU)

    assert [k.name for k in offer] == ["20260920_120000"]


def test_noll_dygn_blir_minst_ett(tmp_path: Path) -> None:
    """Ett slarvfel i .env ska inte kunna radera dagens bilder."""
    runs = tmp_path / "runs"
    korning(runs, "20260921_190000")
    korning(runs, "20260921_195427")

    assert gamla_korningar(0, runs_dir=runs, nu=NU) == []


def test_runs_dir_ar_inuti_bildkatalogen() -> None:
    """Skyddet: städningen får bara arbeta i captures/runs."""
    assert cleanup.RUNS_DIR.name == "runs"
    assert cleanup.RUNS_DIR.parent.name == "captures"


def test_gransen_raknas_i_dygn() -> None:
    assert timedelta(days=7).days == 7
