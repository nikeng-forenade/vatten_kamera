"""Tester för kamerans uppgifter: de ska bara finnas lokalt, och gå att sköta
 från gränssnittet.

Kamerans adress, användare och lösenord ligger i `.env` — som är gitignorerad —
och kan ändras i webbgränssnittet. Ingenting av det får finnas i koden, för
repot kan bli publikt (HACS kräver det). Testerna nedan vaktar båda delarna:

* att ingen kamerauppgift eller IP-adress finns i filer som går till git,
* att kameran säger till tydligt när adressen saknas,
* att kameravägarna i gränssnittet gör rätt saker (utan kamera inkopplad).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import camera_settings
import config
import status_server
from camera import CameraError, HikvisionCamera
from config import CameraConfig

ROT = Path(__file__).resolve().parent.parent

# Filer som foljer med till git och dar kamerans uppgifter INTE far sta.
OFFENTLIGA = (
    "config.py",
    "camera.py",
    "camera_settings.py",
    ".env.example",
    "lxc/install.sh",
    "lxc/proxmox-create.sh",
)

IP_ADRESS = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


# Adresser som inte sager nagot om var kameran star.
OFARLIGA = {"0.0.0.0", "127.0.0.1", "255.255.255.255"}


def test_inga_kamerauppgifter_i_filer_som_gar_till_git() -> None:
    """Ingen IP-adress och inget losenord i kod, exempel eller skript."""
    for namn in OFFENTLIGA:
        text = (ROT / namn).read_text(encoding="utf-8")
        traff = [adress for adress in IP_ADRESS.findall(text) if adress not in OFARLIGA]
        assert traff == [], f"{namn} innehaller en IP-adress: {', '.join(traff)}"


def test_inga_standardvarden_for_kameran_i_koden() -> None:
    """Kameran får inte ha ett inbyggt standardvärde - då hamnar det i repot."""
    text = (ROT / "config.py").read_text(encoding="utf-8")
    assert '_get("CAMERA_IP", "' not in text
    assert '_get("CAMERA_USER", "' not in text


def test_exempelfilen_har_tomma_kamerafalt() -> None:
    rader = (ROT / ".env.example").read_text(encoding="utf-8").splitlines()
    for nyckel in ("CAMERA_IP=", "CAMERA_USER=", "CAMERA_PASSWORD="):
        assert nyckel in rader, f"{nyckel} ska finnas men vara tom i .env.example"


def test_kameran_sager_till_nar_adressen_saknas() -> None:
    """En tom adress ska ge ett begripligt fel - inte ett krångligt socket-fel."""
    with pytest.raises(CameraError) as fel:
        HikvisionCamera(CameraConfig(ip="", user="", password=""))

    assert "adress" in str(fel.value)
    assert "Installningar" in str(fel.value) or "granssnittet" in str(fel.value)


def test_kamerans_installningar_kan_inte_na_kameran_utan_adress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tom = config.load_config()
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(**{**vars(tom), "camera": CameraConfig(ip="", user="", password="")})
    )
    monkeypatch.setattr(camera_settings, "PROFILE_FILE", tmp_path / "finns-inte.json")
    monkeypatch.setattr(camera_settings, "BACKUP_FILE", tmp_path / "finns-inte-backup.json")

    data = status_server.camera_status()

    assert data["ok"] is False
    assert "adress" in data["text"]
    assert data["har_profil"] is False


def test_okand_kameraatgard_avvisas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Utan kamera inkopplad: en påhittad åtgärd ska avvisas innan något skickas."""
    cfg = config.load_config()
    if not cfg.camera.ip:
        pytest.skip("kamerans adress saknas i .env")

    monkeypatch.setattr(camera_settings, "PROFILE_FILE", tmp_path / "finns-inte.json")
    svar = status_server.camera_action("satt-pa-disco")

    assert svar["ok"] is False
    assert "okand" in svar["text"]


def test_laslaget_utan_profil_sager_till(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = config.load_config()
    if not cfg.camera.ip:
        pytest.skip("kamerans adress saknas i .env")

    monkeypatch.setattr(camera_settings, "PROFILE_FILE", tmp_path / "ingen-profil.json")
    svar = status_server.camera_action("lasning")

    assert svar["ok"] is False
    assert "lasprofil" in svar["text"]


def test_kameravagen_visar_laget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Själva vägen: vad kameran svarar ska gå rakt ut till gränssnittet."""
    fake = {"ok": True, "values": {"ircut": "night"}, "har_profil": False, "har_backup": True}
    monkeypatch.setattr(status_server, "camera_status", lambda: fake)

    import socket
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    tjanst = status_server.StatusServer(port=port, bind="127.0.0.1")
    assert tjanst.start() is True
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/camera", timeout=10) as svar:
            data = json.loads(svar.read().decode("utf-8"))
    finally:
        tjanst.stop()

    assert data == fake
