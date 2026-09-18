"""Tester för kameraläget.

Testerna rör inte kameran - de kontrollerar reglerna runt backup och läsprofil,
alltså det som en gång gick fel och gjorde att kamerans eget nattläge inte gick
att få tillbaka.

Kor:  .venv/Scripts/python.exe -m pytest tests -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import camera_settings
from camera_settings import CameraSettings
from config import CameraConfig


def _settings() -> CameraSettings:
    return CameraSettings(CameraConfig(ip="127.0.0.1", user="test", password="test"))


def test_backup_skrivs_inte_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # En backup som skrivs over ar vardelos - da gar det inte att komma tillbaka
    # till utgangslaget.
    backup = tmp_path / "backup.json"
    backup.write_text('{"saved_at": "forsta"}', encoding="utf-8")
    monkeypatch.setattr(camera_settings, "BACKUP_FILE", backup)

    _settings().backup()

    assert "forsta" in backup.read_text(encoding="utf-8"), "backupen skrevs over"


def test_backup_kan_tvingas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup = tmp_path / "backup.json"
    backup.write_text('{"saved_at": "forsta"}', encoding="utf-8")
    monkeypatch.setattr(camera_settings, "BACKUP_FILE", backup)

    settings = _settings()
    settings.values = {"ircut": "night"}
    settings.xml = "<xml/>"

    settings.backup(force=True)

    assert "night" in backup.read_text(encoding="utf-8")


def test_lasprofil_utan_fil_ger_inget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera_settings, "PROFILE_FILE", tmp_path / "saknas.json")
    assert _settings().load_profile() is None


def test_lasprofil_sparas_och_lases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera_settings, "PROFILE_FILE", tmp_path / "profile.json")

    settings = _settings()
    profile = {"ircut": "day", "gain": "20", "shutter": "1/100"}

    path = settings.save_profile(profile)

    assert path.exists()
    assert settings.load_profile() == profile


def test_trasig_lasprofil_kraschar_inte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text("{ inte giltig json", encoding="utf-8")
    monkeypatch.setattr(camera_settings, "PROFILE_FILE", profile)

    assert _settings().load_profile() is None


def test_bara_kanda_installningar_godtas() -> None:
    from camera_settings import CameraSettingsError

    settings = _settings()
    with pytest.raises(CameraSettingsError):
        settings.apply({"finns_inte": "1"})


def test_bara_tillatna_varden_godtas() -> None:
    from camera_settings import CameraSettingsError

    settings = _settings()
    with pytest.raises(CameraSettingsError):
        settings.apply({"ircut": "kvall"})
