"""Tester för installationsskripten till LXC.

Skripten är det enda som körs på en maskin där ingen kan läsa koden och rätta
den efteråt, så de enkla sakerna måste stämma: att sökvägarna pekar på samma
katalog som tjänsten använder, att data-katalogen ligger utanför koden, och att
kalibreringen kan följa med in i containern.

Felet som fångades först: sluttexten i båda skripten hänvisade till
`/opt/vatten-kamera/data/`, men katalogen heter `/opt/vattenkamera` — den som
följde rådet kopierade kalibreringen till en katalog tjänsten aldrig läser.
"""

from __future__ import annotations

import re
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent
LXC = ROT / "lxc"

INSTALL = (LXC / "install.sh").read_text(encoding="utf-8")
CREATE = (LXC / "proxmox-create.sh").read_text(encoding="utf-8")
UPDATE = (LXC / "update.sh").read_text(encoding="utf-8")
SERVICE = (LXC / "vatten-kamera.service").read_text(encoding="utf-8")


def _app_dir(text: str) -> str:
    traff = re.search(r'^APP_DIR="([^"]+)"', text, re.MULTILINE)
    assert traff, "APP_DIR saknas i skriptet"
    return traff.group(1)


def test_alla_skript_anvander_samma_katalog() -> None:
    """Samma APP_DIR i install, update och systemd-enheten."""
    app_dir = _app_dir(INSTALL)
    assert app_dir == _app_dir(UPDATE)
    assert "WorkingDirectory=@APP_DIR@" in SERVICE
    assert 'cd "$APP_DIR"' in UPDATE, "update.sh ska ga till samma katalog"


def test_data_katalogen_ligger_utanfor_koden() -> None:
    """En uppdatering av koden (git reset --hard) far inte rora data."""
    assert 'DATA_DIR="${APP_DIR}/data"' in INSTALL
    assert "Environment=DATA_DIR=" in SERVICE
    assert 'DATA_DIR=${DATA_DIR}' in INSTALL, "data-katalogen ska sta i .env"
    assert "reset --hard" in INSTALL, "install.sh ska kunna koras om"


def test_inga_hittade_pa_kataloger_i_hjalptexterna() -> None:
    """Sluttexten ska peka pa den katalog som faktiskt anvands."""
    for namn, text in (("install.sh", INSTALL), ("proxmox-create.sh", CREATE)):
        assert "/opt/vatten-kamera" not in text, f"{namn} pekar pa fel katalog"
    assert "/opt/vattenkamera/data/" in CREATE
    assert "${DATA_DIR}/" in INSTALL


def test_kalibreringen_kan_folja_med_in_i_containern() -> None:
    """--calibration ska kopieras in, inte skickas vidare som en lokal sokvag."""
    assert "--calibration" in INSTALL
    assert 'cp "$CALIBRATION" "$DATA_DIR/calibration.json"' in INSTALL
    assert 'pct push "$CT_ID" "$CALIBRATION" /root/calibration.json' in CREATE
    assert 'INSTALL_OPTS="$INSTALL_OPTS --calibration /root/calibration.json"' in CREATE


def test_tjansten_anvander_virtualenv_och_kan_startas_om() -> None:
    """Opencv without grafik, och en enhet som granssnittet kan starta om."""
    assert "opencv-python-headless" in INSTALL
    assert 'grep -v \'^opencv-python\'' in INSTALL
    assert "ExecStart=@APP_DIR@/.venv/bin/python main.py daemon" in SERVICE
    assert "Restart=always" in SERVICE
    assert "systemctl restart" in UPDATE


def test_kameran_installeras_utan_gpu() -> None:
    """Containern ar huvudlos - inget skrivbord och inga grafikbibliotek."""
    assert "python3-venv" in INSTALL
    assert "libglib2.0-0" in INSTALL
    assert "x11" not in INSTALL.lower()
