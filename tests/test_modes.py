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
    """En körning som just blev klar (tiderna maste vara i nutid)."""
    nu = datetime.now()
    return RunSummary(
        version="test",
        started=(nu - timedelta(minutes=1)).isoformat(timespec="seconds"),
        finished=nu.isoformat(timespec="seconds"),
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
    # reload skulle lasa den riktiga .env och byta lage mitt i testet.
    monkeypatch.setattr(NightlyRunner, "reload", lambda self: False)
    cfg = SimpleNamespace(run=RunConfig(**run))
    instans = NightlyRunner(cfg)
    instans.cfg = cfg
    return instans


# --- Laget ur .env --------------------------------------------------------


def test_kanda_lagen_lases_som_de_ar() -> None:
    assert _read_mode("natt") == "natt"
    assert _read_mode("intervall") == "intervall"
    assert _read_mode("manuell") == "manuell"


def test_okant_lage_blir_intervall() -> None:
    # Ett slarvfel i .env ska inte kunna stanga av lasningen helt.
    assert _read_mode("") == "intervall"
    assert _read_mode("varje-timme") == "intervall"


def test_standardlaget_laser_hela_tiden() -> None:
    assert RunConfig().mode == "intervall"
    assert RunConfig().every_minutes == 5.0


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
    antal = {"korningar": 0, "nasta": []}

    def fejkad_korning(self: NightlyRunner, **kw: object) -> RunSummary:
        antal["korningar"] += 1
        return fejkad_summary()

    def fejkad_set_state(**kw: object) -> None:
        if kw.get("next_run"):
            antal["nasta"].append(datetime.fromisoformat(str(kw["next_run"])))

    monkeypatch.setattr(NightlyRunner, "run_once_if_free", fejkad_korning)
    monkeypatch.setattr(pipeline, "set_state", fejkad_set_state)
    monkeypatch.setattr(
        NightlyRunner, "wait_until", lambda self, target, **kw: (_ for _ in ()).throw(Klar())
    )

    with pytest.raises(Klar):
        instans._run_forever()

    assert antal["korningar"] == 1
    # Nasta lasning ska ligga ~10 minuter fram i tiden - det granssnittet visar.
    kvar = antal["nasta"][-1] - datetime.now()
    assert timedelta(minutes=9) < kvar <= timedelta(minutes=10)


def test_intervall_kortare_an_en_minut_tillats_inte() -> None:
    instans = object.__new__(NightlyRunner)
    instans.cfg = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=0.1))

    nu = datetime(2026, 9, 21, 12, 0, 0)
    assert NightlyRunner.next_interval_start(instans, after=nu) == nu + timedelta(minutes=1)


def test_intervallet_raknas_fran_senaste_lasningen() -> None:
    """Takten ar lastid + EVERY_MINUTES, inte klockan."""
    instans = object.__new__(NightlyRunner)
    instans.cfg = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=10))

    lasningen_blev_klar = datetime(2026, 9, 21, 12, 3, 30)
    assert NightlyRunner.nasta_efter(instans, lasningen_blev_klar) == datetime(
        2026, 9, 21, 12, 13, 30
    )


def test_andrat_intervall_slar_igenom_under_vantan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sparar man 10 minuter medan tjansten vantar ska nasta lasning flyttas fram."""
    instans = runner(monkeypatch, mode="intervall", every_minutes=5)
    senast = datetime.now()

    # Forsta omgangen ger samma lage (5 min), andra ger det nya (10 min).
    anrop = {"n": 0}

    def fejkad_reload(self: NightlyRunner) -> bool:
        anrop["n"] += 1
        if anrop["n"] >= 2:
            instans.cfg = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=10))
        return anrop["n"] >= 2

    monkeypatch.setattr(NightlyRunner, "reload", fejkad_reload)

    mal: list[datetime] = []

    def fejkad_set_state(**kw: object) -> None:
        if kw.get("next_run"):
            mal.append(datetime.fromisoformat(str(kw["next_run"])))

    def fejkad_vantan(self: NightlyRunner, target: datetime, **kw: object) -> None:
        if len(mal) >= 2:
            raise Klar()

    monkeypatch.setattr(pipeline, "set_state", fejkad_set_state)
    monkeypatch.setattr(NightlyRunner, "wait_until", fejkad_vantan)

    with pytest.raises(Klar):
        NightlyRunner._vanta_till(instans, lambda: instans.nasta_efter(senast), lage="intervall")

    # Forst 5 minuter (gamla vardet), sedan 10 (det nya) - utan omstart.
    assert timedelta(minutes=4) < mal[0] - senast <= timedelta(minutes=5)
    assert timedelta(minutes=9) < mal[1] - senast <= timedelta(minutes=10)


def test_noll_minuter_betyder_hela_tiden() -> None:
    instans = object.__new__(NightlyRunner)
    instans.cfg = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=0))

    nu = datetime(2026, 9, 21, 12, 0, 0)
    nasta = NightlyRunner.next_interval_start(instans, after=nu)
    assert timedelta(seconds=1) < nasta - nu < timedelta(minutes=1)


def test_granssnittet_visar_hela_tiden_som_en_strax_tid() -> None:
    senast = datetime.now() - timedelta(minutes=2)
    nasta = status_server.next_run_at(
        "02:05:00",
        0,
        mode="intervall",
        every_minutes=0,
        last_finished=senast.isoformat(timespec="seconds"),
    )

    assert nasta is not None
    assert timedelta(seconds=4) <= datetime.fromisoformat(nasta) - senast <= timedelta(seconds=6)


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


# --- Andrade installningar utan omstart ----------------------------------


def test_tjansten_laser_om_env_mellan_korningarna(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sparar man i granssnittet ska det sla igenom utan att tjansten startas om."""
    from dataclasses import replace

    import config as config_mod

    grund = config_mod.load_config()
    ny = replace(grund, run=replace(grund.run, mode="manuell", every_minutes=1))

    instans = NightlyRunner.__new__(NightlyRunner)
    instans.cfg = grund
    instans.similarity_threshold = 8.0
    instans.use_camera_profile = False
    instans.camera = instans.ha = instans.mqtt = instans.rest = None
    instans.calibration = None

    monkeypatch.setattr(pipeline, "load_config", lambda: ny)
    # Klienterna byggs om av reload - de behover inte kunna na nagot.
    monkeypatch.setattr(pipeline, "HikvisionCamera", lambda cfg: "kamera")
    monkeypatch.setattr(pipeline, "HomeAssistant", lambda cfg: "ha")
    monkeypatch.setattr(pipeline, "MqttPublisher", lambda cfg: "mqtt")
    monkeypatch.setattr(pipeline, "RestPublisher", lambda ha, unit: "rest")
    monkeypatch.setattr(pipeline.Calibration, "load", lambda path: "kalibrering")

    assert instans.reload() is True
    assert instans.cfg is ny
    assert instans.cfg.run.mode == "manuell"
    assert instans.camera == "kamera"
    assert instans.calibration == "kalibrering"


def test_trasig_env_stoppar_inte_tjansten(monkeypatch: pytest.MonkeyPatch) -> None:
    import config as config_mod

    grund = config_mod.load_config()
    instans = NightlyRunner.__new__(NightlyRunner)
    instans.cfg = grund

    def spräng() -> None:
        raise ValueError("nagot ar fel i .env")

    monkeypatch.setattr(pipeline, "load_config", spräng)

    assert instans.reload() is False
    assert instans.cfg is grund  # de gamla installningarna galler vidare


def test_versiontexten_namner_klockslaget_bara_i_lage_natt() -> None:
    """RUN_AT ar bara en vackningstid i lage natt.

    Vardet tas anda fran sidan efter att displayen visat 02:00, sa att skriva
    "kor kl 02:05" i lage intervall fick det att se ut som att lasningen vantar
    pa en klockslag.
    """
    from main import _lasbeskrivning

    intervall = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=10))
    text = _lasbeskrivning(intervall)
    assert "02:05" not in text
    assert "var 10 minut" in text

    hela_tiden = SimpleNamespace(run=RunConfig(mode="intervall", every_minutes=0))
    assert "hela tiden" in _lasbeskrivning(hela_tiden)

    manuell = SimpleNamespace(run=RunConfig(mode="manuell"))
    assert "Las nu" in _lasbeskrivning(manuell)

    natt = SimpleNamespace(run=RunConfig(mode="natt", run_at="02:05:00"))
    assert "02:05" in _lasbeskrivning(natt)
