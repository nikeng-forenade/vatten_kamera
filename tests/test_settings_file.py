"""Tester for att flytta installningarna mellan maskiner.

Verktyget finns for att en LXC ska fa samma inmatade installningar som maskinen
dar lasningen provades ut - men **utan losenord och tokens**. Testerna vaktar
tva saker: att hemligheterna aldrig hamnar i filen, och att en inlasning inte
ror ett redan ifyllt losenord pa den nya maskinen.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import settings_file

ROT = Path(__file__).resolve().parent.parent

ENV = """# Kameran
CAMERA_IP=10.0.0.5
CAMERA_USER=admin
CAMERA_PASSWORD=hemligt
CAMERA_RTSP_PATH=/Streaming/Channels/1

# Displayen
COLOR_CHANNEL=gray
THRESHOLD=100

HA_TOKEN=token-123
"""


def _skriv_env(tmp_path: Path, text: str = ENV) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_exporten_lamnar_hemligheterna_utanfor(tmp_path: Path) -> None:
    """Losenord och tokens ska inte kunna hamna i filen - inte ens oavsiktligt."""
    env = _skriv_env(tmp_path)
    kalibrering = tmp_path / "calibration.json"
    kalibrering.write_text('{"roi": [1, 2, 3, 4]}', encoding="utf-8")

    data = settings_file.samla(
        env=env,
        kalibrering=kalibrering,
        profil=tmp_path / "camera_profile.json",
        backup=tmp_path / "camera_settings_backup.json",
    )

    assert "CAMERA_PASSWORD" not in data["env"]
    assert "HA_TOKEN" not in data["env"]
    assert set(data["hemligheter_utelamnade"]) == {"CAMERA_PASSWORD", "HA_TOKEN"}
    # Adressen och de inmatta vardena ska med - det ar hela poangen.
    assert data["env"]["CAMERA_IP"] == "10.0.0.5"
    assert data["env"]["COLOR_CHANNEL"] == "gray"
    assert data["env"]["THRESHOLD"] == "100"
    assert data["env"]["CAMERA_RTSP_PATH"]  # nycklar utanfor granssnittet ocksa
    assert data["kalibrering"]["roi"] == [1, 2, 3, 4]
    assert "hemligt" not in json.dumps(data)
    assert "token-123" not in json.dumps(data)


def test_importen_ror_inte_ett_ifyllt_losenord(tmp_path: Path) -> None:
    """Den nya maskinens losenord ska sta kvar - aven om filen har ett varde."""
    env = _skriv_env(tmp_path)
    data = {
        "env": {
            "COLOR_CHANNEL": "b",
            "THRESHOLD": "250",
            "CAMERA_PASSWORD": "annat-losenord",
            "HA_TOKEN": "annat-token",
        }
    }

    rapport = settings_file.skriv(data, env=env, kalibrering=tmp_path / "kal.json")

    text = env.read_text(encoding="utf-8")
    assert "COLOR_CHANNEL=b" in text
    assert "THRESHOLD=250" in text
    assert "CAMERA_PASSWORD=hemligt" in text  # orord
    assert "annat-losenord" not in text
    assert "annat-token" not in text
    assert rapport["hemligheter"] == ["CAMERA_PASSWORD", "HA_TOKEN"]


def test_importen_skriver_kalibrering_och_kamerafiler(tmp_path: Path) -> None:
    """Kalibreringen och kamerans filer ska med - och den gamla sparas som .bak."""
    env = _skriv_env(tmp_path)
    kalibrering = tmp_path / "calibration.json"
    kalibrering.write_text('{"roi": [9, 9, 9, 9]}', encoding="utf-8")
    profil = tmp_path / "camera_profile.json"
    backup = tmp_path / "camera_settings_backup.json"

    data = {
        "env": {"THRESHOLD": "250"},
        "kalibrering": {"roi": [985, 0, 1620, 200]},
        "lasprofil": {"values": {"ircut": "day"}},
        "kamerabackup": {"camera": "10.0.0.5"},
    }

    rapport = settings_file.skriv(
        data, env=env, kalibrering=kalibrering, profil=profil, backup=backup
    )

    assert json.loads(kalibrering.read_text(encoding="utf-8"))["roi"] == [985, 0, 1620, 200]
    assert json.loads(kalibrering.with_name("calibration.json.bak").read_text(encoding="utf-8"))[
        "roi"
    ] == [9, 9, 9, 9]
    assert profil.exists() and backup.exists()
    assert rapport["filer"]["kalibrering"] == "skriven"


def test_torrkorning_skriver_ingenting(tmp_path: Path) -> None:
    env = _skriv_env(tmp_path)
    innan = env.read_text(encoding="utf-8")
    kalibrering = tmp_path / "calibration.json"

    rapport = settings_file.skriv(
        {"env": {"THRESHOLD": "250"}, "kalibrering": {"roi": [1, 2, 3, 4]}},
        env=env,
        kalibrering=kalibrering,
        torr=True,
    )

    assert env.read_text(encoding="utf-8") == innan
    assert not kalibrering.exists()
    assert rapport["torr"] is True


def test_ovriga_nycklar_hamnar_i_env(tmp_path: Path) -> None:
    """Nycklar som inte finns i granssnittet ska anda kunna flyttas."""
    env = _skriv_env(tmp_path, "COLOR_CHANNEL=b\n")

    settings_file.skriv(
        {"env": {"CAMERA_RTSP_PATH": "/Streaming/Channels/2", "COLOR_CHANNEL": "b"}},
        env=env,
        kalibrering=tmp_path / "kal.json",
    )

    text = env.read_text(encoding="utf-8")
    assert "CAMERA_RTSP_PATH=/Streaming/Channels/2" in text
    assert text.count("COLOR_CHANNEL=") == 1  # inte tva rader av samma nyckel


def test_tomma_varden_foljer_inte_med(tmp_path: Path) -> None:
    """En tom rad ar inte en installning - den far inte skriva over nagot.

    Utan det har skulle '--unit l' fran installationen raderas av ett tomt UNIT i
    .env pa maskinen som filen kom ifran.
    """
    env = _skriv_env(tmp_path, "UNIT=\nHA_LIGHT_ENTITY=\nCOLOR_CHANNEL=b\n")
    data = settings_file.samla(env=env, kalibrering=tmp_path / "saknas.json")

    assert data["env"] == {"COLOR_CHANNEL": "b"}
    assert sorted(data["tomma_utelamnade"]) == ["HA_LIGHT_ENTITY", "UNIT"]


def test_exportfilen_ar_gitignorerad() -> None:
    """Filen innehaller kamerans adress - den ska aldrig kunna committas."""
    rader = (ROT / ".gitignore").read_text(encoding="utf-8").split()
    assert settings_file.STANDARD_FIL in rader


def test_maskinspecifika_sokvagar_foljer_inte_med(tmp_path: Path) -> None:
    """Var installationen bor ska inte flyttas - bara hur den laser.

    Ett DATA_DIR fran en annan maskin skulle peka tjansten fel, sa bade exporten
    och inlasningen hoppar over dem.
    """
    env = _skriv_env(tmp_path, "COLOR_CHANNEL=b\nDATA_DIR=/nagon/annanstans\nLOG_FILE=/tmp/x.log\n")
    data = settings_file.samla(env=env, kalibrering=tmp_path / "saknas.json")
    assert "DATA_DIR" not in data["env"]
    assert "LOG_FILE" not in data["env"]

    env2 = tmp_path / "ny.env"
    env2.write_text("COLOR_CHANNEL=gray\n", encoding="utf-8")
    rapport = settings_file.skriv(
        {"env": {"DATA_DIR": "/nagon/annanstans", "COLOR_CHANNEL": "b"}},
        env=env2,
        kalibrering=tmp_path / "kal.json",
    )
    assert "DATA_DIR" not in env2.read_text(encoding="utf-8")
    assert "COLOR_CHANNEL=b" in env2.read_text(encoding="utf-8")
    assert rapport["maskinnycklar"] == ["DATA_DIR"]
