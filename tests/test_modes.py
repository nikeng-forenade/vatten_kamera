"""Tester for nar lasningen ska starta.

Tjansten kan jobba pa tre satt:

    natt      - en gang per dygn, strax innan RUN_AT
    intervall - direkt vid start och sedan var EVERY_MINUTES minut
    manuell   - bara nar nagon trycker "Las nu" (granssnittet eller HA)

Testerna kor den riktiga koden utan att kameran eller natet behovs: korningen
fejkas och vantan avbryts med ett undantag nar den borjar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import pipeline
import status_server
from config import RunConfig, _read_mode
from pipeline import NightlyRunner, RunSummary


class Klar(Exception):
    """Avbryter vantan i testet."""


def fejkad_summary() -> RunSummary:
    return RunSummary(
        version="test",
        started="2026-09-21T02:05:00",
        finished="2026-09-21T02:06:00",
        frames_taken=10,
        frames_readable=10,
        value="058",
        numeric=0.58,
        votes=4,
        confidence=0.8,
        lamp_used=False,
    )


def runner(monkeypatch: pytest.MonkeyPatch, **run: object) -> NightlyRunner:
    """En korning utan kamera: bara tiderna och laget ar intressanta."""
    monkeypatch.setattr(NightlyRunner, "__init__", lambda self, cfg, **kw: None)
    cfg = SimpleNamespace(run=RunConfig(**run))
    instans = NightlyRunner(cfg)
    instans.cfg = cfg
    return instans


# --- Laget ur .env --------------------------------------------------------


def test_kanda_lagen_lases_som_de_ar() -> None:
    assert _read_mode("natt") == "natt"
    assert _read_mode("intervall") == "intervall"
    assert _read_mode("manuell") == "manuell"


def test_okant_lage_blir_natt() -> None:
    # Ett slarvfel i .env ska inte kunna stanga av lasningen helt.
    assert _read_mode("") == "natt"
    assert _read_mode("varje-timme") == "natt"


def test_standardlaget_ar_natt() -> None:
    assert RunConfig().mode == "natt"
    assert RunConfig().every_minutes == 10.0


# --- Nasta korning i granssnittet ----------------------------------------


def test_manuellt_lage_har_ingen_nasta_korning() -> None:
    assert status_server.next_run_at("02:05:00", 600, mode="manuell") is None


def test_intervall_raknas_fran_senaste_lasningen() -> None:
    senast = datetime.now() - timedelta(minutes=3)
    nasta = status_server.next_run_at(
        "02:05:00",
        600,
        mode="intervall",
        every_minutes=10,
        last_finished=senast.isoformat(timespec="seconds"),
    )
    assert nasta is not None
    vantan = datetime.fromisoformat(nasta) - senast
    # Tidsstampeln skrivs utan mikrosekunder - en sekund hit eller dit ar ok.
    assert timedelta(minutes=9, seconds=59) <= vantan <= timedelta(minutes=10, seconds=1)


def test_intervall_utan_tidigare_lasning_ger_ingen_tid() -> None:
    assert status_server.next_run_at("02:05:00", 0, mode="intervall", last_finished=None) is None


def test_intervall_med_trasig_tid_ger_ingen_tid() -> None:
    assert (
        status_server.next_run_at("02:05:00", 0, mode="intervall", last_finished="inte en tid")
        is None
    )


# --- Korningen ------------------------------------------------------------


def test_manuellt_lage_laser_aldrig_av_sig_sjalv(monkeypatch: pytest.MonkeyPatch) -> None:
    instans = runner(monkeypatch, mode="manuell")
    monkeypatch.setattr(
        NightlyRunner, "run_once", lambda self, **kw: pytest.fail("fick inte lasa")
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: (_ for _ in ()).throw(Klar()))

    with pytest.raises(Klar):
        instans._run_forever()

    # Tjansten ska inte utlova nagon tid i det laget.
    assert status_server.get_state().get("next_run") is None


def test_intervall_laser_och_vantar_var_x_minut(monkeypatch: pytest.MonkeyPatch) -> None:
    instans = runner(monkeypatch, mode="intervall", every_minutes=10)
    antal = {"korningar": 0, "vantat_till": []}

    def fejkad_korning(self: NightlyRunner, **kw: object) -> RunSummary:
        antal["korningar"] += 1
        return fejkad_summary()

    def fejkad_vantan(self: NightlyRunner, target: datetime) -> None:
        antal["vantat_till"].append(target)
        raise Klar()

    monkeypatch.setattr(NightlyRunner, "run_once_if_free", fejkad_korning)
    monkeypatch.setattr(NightlyRunner, "wait_until", fejkad_vantan)

    with pytest.raises(Klar):
        instans._run_forever()

    assert antal["korningar"] == 1
    # Vantan ska ligga ~10 minuter fram i tiden.
    kvar = antal["vantat_till"][0] - datetime.now()
    assert timedelta(minutes=9) < kvar <= timedelta(minutes=10)


def test_intervall_kortare_an_en_minut_tillats_inte() -> None:
    instans = object.__new__(NightlyRunner)
    instans.cfg = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=0.1))

    nu = datetime(2026, 9, 21, 12, 0, 0)
    assert NightlyRunner.next_interval_start(instans, after=nu) == nu + timedelta(minutes=1)


def test_schemalagd_korning_hoppar_over_nar_en_annan_laser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instans = runner(monkeypatch, mode="intervall")
    monkeypatch.setattr(
        NightlyRunner, "run_once", lambda self, **kw: pytest.fail("fick inte kora samtidigt")
    )
    monkeypatch.setattr(
        status_server, "get_state", lambda: {"running": True}
    )

    assert instans.run_once_if_free() is None


def test_schemalagd_korning_kor_nar_ingen_annan_ar_i_gang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instans = runner(monkeypatch, mode="natt")
    monkeypatch.setattr(status_server, "get_state", lambda: {"running": False})
    monkeypatch.setattr(NightlyRunner, "run_once", lambda self, **kw: fejkad_summary())

    summary = instans.run_once_if_free()

    assert summary is not None
    assert summary.value == "058"
