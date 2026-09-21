"""Tester for webbgranssnittet.

Tjansten startas pa en ledig port och provas med riktiga HTTP-anrop, sa att
bade vaggarna och svaren kontrolleras utan att nagon webblasare behovs.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import status_server
from status_server import StatusServer, frame_url, next_run_at, read_latest, tail_log


def ledig_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """En tjanst med egna filer, sa att testerna inte ror riktiga installationen."""
    latest = tmp_path / "latest.json"
    loggen = tmp_path / "app.log"
    latest.write_text(
        json.dumps(
            {
                "ok": True,
                "value": "058",
                "numeric": 0.58,
                "display": "0.58",
                "unit": "l",
                "confidence": 0.85,
                "votes": 17,
                "frames": 70,
                "read_at": "2026-09-21T17:51:10",
                "bild": str(tmp_path / "runs" / "bild.jpg"),
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    loggen.write_text("rad ett\nrad tva\n", encoding="utf-8")
    monkeypatch.setattr(status_server, "LATEST_FILE", latest)
    monkeypatch.setattr(status_server, "LOG_FILE", loggen)
    monkeypatch.setattr(status_server, "CAPTURES_DIR", tmp_path)

    tjanst = StatusServer(port=ledig_port(), bind="127.0.0.1")
    assert tjanst.start() is True
    yield tjanst
    tjanst.stop()


def hamta(server: StatusServer, path: str, body: dict | None = None) -> tuple[int, dict | str | bytes]:
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
            if "application/json" in content_type:
                return response.status, json.loads(raw.decode("utf-8"))
            if content_type.startswith("text/"):
                return response.status, raw.decode("utf-8")
            return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        content_type = exc.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return exc.code, json.loads(raw.decode("utf-8"))
        return exc.code, raw


def test_granssnittet_serveras(server: StatusServer) -> None:
    status, html = hamta(server, "/")

    assert status == 200
    assert "Vattenkamera" in html
    assert "/api/health" in html


def test_health_sager_att_tjansten_lever(server: StatusServer) -> None:
    status, data = hamta(server, "/api/health")

    assert status == 200
    assert data["ok"] is True
    assert data["kor"] is False
    assert data["nasta_korning"]  # en tid, oavsett nar testet kors


def test_latest_ger_vardet_och_en_bildadress(server: StatusServer) -> None:
    status, data = hamta(server, "/api/latest")

    assert status == 200
    assert data["numeric"] == 0.58
    assert data["votes"] == 17


def test_bilden_serveras_men_inte_filer_utanfor(server: StatusServer) -> None:
    bild = status_server.CAPTURES_DIR / "runs" / "bild.jpg"
    bild.parent.mkdir(parents=True, exist_ok=True)
    bild.write_bytes(b"\xff\xd8\xff\xd9")

    status, _ = hamta(server, "/api/frames/runs/bild.jpg")
    assert status == 200

    status, data = hamta(server, "/api/frames/../../etc/passwd")
    assert status in {403, 404}


def test_config_visar_falt_utan_hemliga_varden(server: StatusServer) -> None:
    status, data = hamta(server, "/api/config")

    assert status == 200
    falt = {item["key"]: item for item in data["fields"]}
    assert falt["RUN_AT"]["label"]
    assert falt["HA_TOKEN"]["kind"] == "secret"


def test_loggen_ger_sista_raderna(server: StatusServer) -> None:
    status, data = hamta(server, "/api/log?lines=1")

    assert status == 200
    assert data["lines"] == ["rad tva"]


def test_okand_vag_ger_404(server: StatusServer) -> None:
    status, data = hamta(server, "/api/finns-inte")

    assert status == 404
    assert data["ok"] is False


@pytest.mark.parametrize("vad", ["camera", "ha", "mqtt"])
def test_testvagarna_svarar(server: StatusServer, monkeypatch: pytest.MonkeyPatch, vad: str) -> None:
    """Vaggarna ska svara - proven mot kamera och HA stamplas har, sa att testet
    inte ror nagon riktig maskinvara."""
    monkeypatch.setattr(
        status_server,
        f"test_{vad}",
        lambda: {"ok": True, "text": f"provsvar fran {vad}"},
    )

    status, data = hamta(server, f"/api/test/{vad}")

    assert status == 200
    assert data["ok"] is True
    assert vad in data["text"]


def test_trasigt_test_kraschar_inte_tjansten(server: StatusServer, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict:
        raise RuntimeError("kameran svarar inte alls")

    monkeypatch.setattr(status_server, "test_camera", boom)

    status, data = hamta(server, "/api/test/camera")

    assert status == 200
    assert data["ok"] is False
    assert "kraschade" in data["text"]


def test_latest_utan_fil_sager_att_ingen_lasning_gjorts(tmp_path: Path) -> None:
    data = read_latest(tmp_path / "saknas.json")

    assert data["ok"] is False
    assert "ingen lasning" in data["error"]


def test_logg_utan_fil_ger_en_forklaring(tmp_path: Path) -> None:
    rader = tail_log(10, tmp_path / "saknas.log")

    assert len(rader) == 1
    assert "ingen loggfil" in rader[0]


def test_bildadress_bara_innanfor_bilder() -> None:
    assert frame_url(None) is None
    assert frame_url("C:/nagon/annanstans/bild.jpg") is None


def test_nasta_korning_ar_framat_i_tiden() -> None:
    from datetime import datetime

    tid = next_run_at("02:05:00", 10)

    assert tid is not None
    assert datetime.fromisoformat(tid) > datetime.now()


def test_health_sager_vad_granssnittet_far_gora(server: StatusServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # Testet satter flaggorna sjalv, sa att det inte beror pa vad som star i
    # .env pa maskinen som kor testerna.
    monkeypatch.setattr(status_server, "STATUS_LIVE", True)
    monkeypatch.setattr(status_server, "STATUS_ALLOW_RUN", True)
    monkeypatch.setattr(status_server, "STATUS_ALLOW_RESTART", True)

    _, data = hamta(server, "/api/health")

    assert data["live"] is True
    assert data["far_lasa"] is True
    assert data["far_starta_om"] is True


def test_lasning_direkt_kan_stangas_av(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_server, "STATUS_ALLOW_RUN", False)
    monkeypatch.setattr(status_server, "STATUS_LIVE", False)
    tjanst = StatusServer(port=ledig_port(), bind="127.0.0.1")
    assert tjanst.start() is True
    try:
        _, health = hamta(tjanst, "/api/health")
        assert health["live"] is False
        assert health["far_lasa"] is False

        status, data = hamta(tjanst, "/api/run", {})
        assert status == 403
        assert "avstangd" in data["text"]
    finally:
        tjanst.stop()


def test_omstart_kan_stangas_av(server: StatusServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_server, "STATUS_ALLOW_RESTART", False)

    status, data = hamta(server, "/api/restart", {})

    assert status == 403
    assert "avstangd" in data["text"]


def test_granssnittet_visar_senaste_last_varde_aven_efter_omstart(server: StatusServer) -> None:
    # Vardet ligger i latest.json, sa panelen visar det aven nar tjansten just
    # startat och ingen lasning gjorts i den har processen.
    status, data = hamta(server, "/api/latest")

    assert status == 200
    assert data["display"] == "0.58"
    assert data["unit"] == "l"
    assert data["confidence"] == 0.85
    assert data["read_at"] == "2026-09-21T17:51:10"
