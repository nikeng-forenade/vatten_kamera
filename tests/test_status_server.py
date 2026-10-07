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
    # Tiden beror pa laget i .env (i lage manuell vantar ingen tid alls), sa
    # testet kollar att nycklarna finns - inte vad klockan står pa.
    assert "nasta_korning" in data
    assert data["lage"] in {"natt", "intervall", "manuell"}


def test_latest_ger_vardet_och_en_bildadress(server: StatusServer) -> None:
    status, data = hamta(server, "/api/latest")

    assert status == 200
    assert data["numeric"] == 0.58
    assert data["votes"] == 17


def test_health_skiljer_senaste_forsoket_fran_vardet(server: StatusServer) -> None:
    """Vardet kan vara ett aldre, giltigt varde - health sager om senaste forsoket.

    Granssnittet och Home Assistant visar vardet aven nar en korning missar
    (tjansten behaller det senaste). Utan den har raden gar det inte att se att
    korningen missade, och da ser allt ut att vara bra.
    """
    status, data = hamta(server, "/api/health")

    assert status == 200
    assert data["lasning_ok"] is True



def test_health_sager_att_senaste_forsoket_missade(server: StatusServer) -> None:
    """En missad korning ska synas i health aven om vardet star kvar."""
    fil = status_server.LATEST_FILE
    data = json.loads(fil.read_text(encoding="utf-8"))
    data["lasning_ok"] = False
    data["senaste_forsok"] = {
        "ok": False,
        "read_at": "2026-09-29T06:35:12",
        "error": "for fa eniga lasningar",
    }
    fil.write_text(json.dumps(data), encoding="utf-8")

    status, health = hamta(server, "/api/health")

    assert status == 200
    assert health["lasning_ok"] is False
    assert health["senaste_forsok"]["read_at"] == "2026-09-29T06:35:12"


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


def test_historiken_ger_punkter_for_grafen(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Grafen ritas ur /api/history - varje punkt ska ha varde, tid och bild."""
    import history
    from datetime import datetime

    bild = status_server.CAPTURES_DIR / "runs" / "graf.jpg"
    bild.parent.mkdir(parents=True, exist_ok=True)
    bild.write_bytes(b"\xff\xd8\xff\xd9")
    fil = tmp_path / "history.jsonl"
    # Nyss, inte ett fast datum: /api/history filtrerar pa de senaste timmarna.
    nu = datetime.now().replace(microsecond=0)
    history.write(
        [
            {
                "read_at": nu.isoformat(),
                "numeric": 0.57,
                "display": "0.57",
                "ok": True,
                "confidence": 0.83,
                "votes": 3,
                "frames": 19,
                "bild": str(bild),
                "error": "",
            }
        ],
        path=fil,
    )
    monkeypatch.setattr(history, "HISTORY_FILE", fil)

    status, data = hamta(server, "/api/history?hours=24")

    assert status == 200
    assert data["antal"] == 1
    punkt = data["punkter"][0]
    assert punkt["varde"] == 0.57
    assert punkt["visas_som"] == "0.57"
    assert punkt["bild_url"] == "/api/frames/runs/graf.jpg"


def test_health_berattar_om_kalibreringen_finns(server: StatusServer) -> None:
    """Utan kalibrering kan tjansten inte lasa - det ska ga att se i granssnittet."""
    status, data = hamta(server, "/api/health")

    assert status == 200
    assert isinstance(data["kalibrering"], bool)


def test_health_berattar_om_flodet_och_lackage(server: StatusServer) -> None:
    """Granssnittet och Home Assistant ska kunna se om nagot rinner hela tiden."""
    status, data = hamta(server, "/api/health")

    assert status == 200
    assert isinstance(data["lackage"], bool)
    assert data["lackage_text"]
    assert data["lackage_troskel"] > 0
    assert data["lackage_minuter"] > 0
    assert data["flode_enhet"]


def test_historiken_ger_flodet_for_grafen(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Grafen ska kunna rita flodet som en egen linje."""
    import history
    from datetime import datetime

    fil = tmp_path / "history.jsonl"
    nu = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    history.write(
        [
            {
                "read_at": nu,
                "numeric": 0.37,
                "display": "0.37",
                "ok": True,
                "flow_numeric": 0.12,
                "flow": "0.12",
            }
        ],
        path=fil,
    )
    monkeypatch.setattr(history, "HISTORY_FILE", fil)

    status, data = hamta(server, "/api/history?hours=24")

    assert status == 200
    assert data["punkter"][0]["flode"] == 0.12


def test_granssnittet_varnar_nar_kalibreringen_saknas() -> None:
    """Varningen ska finnas bade i sidan och styras av svaret fran /api/health."""
    from pathlib import Path

    sida = (Path(status_server.__file__).resolve().parent / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="kalibreringVarning"' in sida
    assert "health.kalibrering" in sida


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

    tid = next_run_at("02:05:00", 10, mode="natt")

    assert tid is not None
    assert datetime.fromisoformat(tid) > datetime.now()


def test_sparade_installningar_sager_om_omstart_behovs(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sparar man en troskel ska svaret saga att det galler direkt - inte be om
    en omstart (som i en LXC startar om hela tjansten)."""
    import settings_store

    env = tmp_path / ".env"
    env.write_text("THRESHOLD=250\nSTATUS_PORT=8099\n", encoding="utf-8")
    monkeypatch.setattr(settings_store, "ENV_FILE", env)

    status, data = hamta(server, "/api/config", {"THRESHOLD": "240"})
    assert status == 200
    assert data["andrade"] == ["THRESHOLD"]
    assert data["omstart_kravs"] == []
    assert "nasta lasning" in data["text"]
    assert env.read_text(encoding="utf-8").splitlines()[0] == "THRESHOLD=240"

    status, data = hamta(server, "/api/config", {"STATUS_PORT": "9000"})
    assert status == 200
    assert data["omstart_kravs"] == ["STATUS_PORT"]
    assert "startas om" in data["text"]


def test_health_sager_vad_granssnittet_far_gora(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch
) -> None:
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


# --- Kalibreringen: flytta rutorna i granssnittet -------------------------
#
# Kameran sitter i en kallare och far en knuff nar saltet fylls pa. Da pekar
# rutorna fel och inget varde publiceras, sa rutorna maste ga att flytta utan att
# mata pa en annan maskin. Testerna kor mot syntetiska bilder i stallet for
# kameran.


def syntetiska_bilder(text: str = " 132", antal: int = 3):
    """Bilder som ser ut som displayen, och rutorna siffrorna ritades i."""
    import cv2
    import numpy as np

    from segments import render_number

    bilder = []
    rutor: list[tuple[int, int, int, int]] = []
    for _ in range(antal):
        canvas, boxes = render_number(text, digit_width=60, digit_height=110)
        bilder.append(
            cv2.cvtColor(np.clip(canvas * 255.0, 0, 255).astype("uint8"), cv2.COLOR_GRAY2BGR)
        )
        rutor = [tuple(box) for box in boxes]
    return bilder, rutor


@pytest.fixture
def kalibreringen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """En egen kalibreringsfil och .env, sa att testerna inte ror installationen."""
    import config
    import settings_store
    from config import ReaderConfig
    from types import SimpleNamespace

    bilder, rutor = syntetiska_bilder()
    hojd, bredd = bilder[0].shape[:2]
    fil = tmp_path / "calibration.json"
    fil.write_text(
        json.dumps(
            {
                "roi": [0, 0, bredd, hojd],
                "digit_count": 4,
                "cell_boxes": [list(box) for box in rutor],
                "notes": "test",
            }
        ),
        encoding="utf-8",
    )
    cfg = SimpleNamespace(
        reader=ReaderConfig(
            digit_count=4, upscale=1.0, decimals=2, require_blank_first=True
        ),
        calibration_roi=(0, 0, bredd, hojd),
    )
    monkeypatch.setattr(status_server, "_kalibreringsfil", lambda: fil)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(settings_store, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(status_server, "_ta_bilder", lambda *a, **kw: syntetiska_bilder()[0])
    monkeypatch.setattr(
        status_server, "_BILD", {"preview": None, "frames": [], "jpeg": None, "tagen": None}
    )
    return fil


def test_kalibreringsvyn_visar_bilden_och_rutorna(server: StatusServer, kalibreringen: Path) -> None:
    status, data = hamta(server, "/api/calibration")

    assert status == 200
    assert data["ok"] is True
    assert len(data["cell_boxes"]) == 4
    # Vyn visar ROI:n med luft runt omkring, sa att en ruta som hamnat utanfor syns.
    vy, roi = data["vy"], data["roi"]
    assert vy[0] <= roi[0] and vy[1] <= roi[1] and vy[2] >= roi[2] and vy[3] >= roi[3]
    assert data["bild_url"].startswith("/api/calibration/bild")
    # Sidan visar en vardesida: vardet star i position 2-4.
    assert data["lasning"]["display"] == "1.32"
    assert [siffra["tecken"] for siffra in data["lasning"]["siffror"]] == [" ", "1", "3", "2"]

    status, bild = hamta(server, data["bild_url"])
    assert status == 200
    assert isinstance(bild, bytes)
    assert bild[:2] == b"\xff\xd8", "bilden ska vara en JPEG"
    assert "skarpaste" in data["text"]


def test_kalibreringsbilden_valjer_en_skarp_enstaka_bild() -> None:
    import cv2
    import numpy as np

    sharp = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.rectangle(sharp, (20, 20), (80, 80), (255, 255, 255), thickness=4)
    blurred = cv2.GaussianBlur(sharp, (15, 15), 0)

    assert status_server._skarpaste_bild([blurred, sharp]) is sharp


def test_kalibreringen_sparas_i_verktygens_format(server: StatusServer, kalibreringen: Path) -> None:
    """Filen maste se ut som tools/fit_cells.py skriver den - samma lasare."""
    nya = [
        [1016, 0, 1112, 100],
        [1136, 13, 1232, 113],
        [1254, 21, 1349, 121],
        [1367, 37, 1462, 137],
    ]

    status, data = hamta(server, "/api/calibration", {"cell_boxes": nya})

    assert status == 200
    assert data["ok"] is True
    sparad = json.loads(kalibreringen.read_text(encoding="utf-8"))
    assert sparad["cell_boxes"] == nya
    assert sparad["digit_count"] == 4
    assert sparad["roi"] == data["roi"]
    assert sparad["notes"]
    assert "lasning" in data


def test_kalibreringen_vagrar_orimliga_rutor(server: StatusServer, kalibreringen: Path) -> None:
    """En ruta som ar fel ger en tyst fel lasning - hellre ett tydligt nej."""
    status, data = hamta(server, "/api/calibration", {"cell_boxes": [[0, 0, 5, 5]] * 4})
    assert status == 400
    assert "for liten" in data["text"]

    status, data = hamta(server, "/api/calibration", {"cell_boxes": [[0, 0, 60, 60]]})
    assert status == 400
    assert "4 rutor" in data["text"]


def test_utsnittet_vaxer_inte_nar_rutorna_ligger_inuti(
    server: StatusServer, kalibreringen: Path
) -> None:
    """Annars vaxer ROI:n en bit varje gang man sparar."""
    fore = json.loads(kalibreringen.read_text(encoding="utf-8"))["roi"]
    rutor = [[10, 5, 70, 105], [80, 5, 140, 105], [150, 5, 210, 105], [200, 5, 260, 105]]

    status, data = hamta(server, "/api/calibration", {"cell_boxes": rutor})

    assert status == 200
    assert data["roi"] == fore, "utsnittet ska inte vaxa nar rutorna ryms"
    assert "vidgat" not in data["text"]


def test_utsnittet_vidgas_nar_en_ruta_hamnar_utanfor(
    server: StatusServer, kalibreringen: Path
) -> None:
    """ROI:n klipper bilden innan rutorna far se den - den maste rymma rutorna."""
    import settings_store

    rutor = [
        [1400, 300, 1500, 400],
        [1600, 300, 1700, 400],
        [1800, 300, 1900, 400],
        [2000, 300, 2100, 400],
    ]

    status, data = hamta(server, "/api/calibration", {"cell_boxes": rutor})

    assert status == 200
    assert data["roi"][2] >= 2100, "ROI:n rymmer inte sista rutan"
    assert data["roi"][3] >= 400
    sparad = json.loads(kalibreringen.read_text(encoding="utf-8"))
    assert sparad["roi"] == data["roi"]
    env = settings_store.ENV_FILE.read_text(encoding="utf-8")
    assert "CALIBRATION_ROI" in env, "verktygen mater mot ROI:n i .env"
    assert "vidgat" in data["text"]


def test_mat_automatiskt_mater_fram_rutorna(server: StatusServer, monkeypatch: pytest.MonkeyPatch, kalibreringen: Path) -> None:
    """Samma vag som 'main.py calibrate' - utan att man behover komma at maskinen."""
    # Fyra tända siffror: matningen behover en siffra i varje position.
    monkeypatch.setattr(status_server, "_ta_bilder", lambda *a, **kw: syntetiska_bilder("1320")[0])

    status, data = hamta(server, "/api/calibration/mat", {})

    assert status == 200
    assert data["ok"] is True
    assert len(data["cell_boxes"]) == 4
    assert data["rapport"], "matningen ska beratta vad den gjorde"


def test_vyn_ror_inte_kameran_medan_en_lasning_pagar(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch, kalibreringen: Path
) -> None:
    """Kameran svarar bara en i taget: att bara oppna panelen far inte stora en
    lasning - och en avbruten lasning hinner inte se hela sidvarvet."""

    def bom(*a: object, **kw: object) -> list[object]:
        raise AssertionError("kameran skulle inte roras")

    monkeypatch.setattr(status_server, "_ta_bilder", bom)
    status_server.set_state(running=True)
    try:
        status, data = hamta(server, "/api/calibration")
    finally:
        status_server.set_state(running=False)

    assert status == 200
    assert data["bild_url"] == "", "ingen bild tas medan lasningen pagar"
    assert "lasning pagar" in data["text"]


def test_kalibreringen_ber_lasningen_slappa_kameran(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En korning som letar forgaves kan halla pa i 30 min - den maste slappa.

    Kameran svarar bara en i taget, sa kalibreringen ber korningen sluta ta
    bilder och vantar tills den ar klar.
    """
    import pipeline

    pipeline._AVBRYT.clear()  # noqa: SLF001 - flaggan ar sjalva saken har
    status_server.set_state(running=False)

    assert status_server.vanta_pa_kameran(timeout_s=0.1) is True
    assert pipeline.avbrott_begart() is False, "ingen lasning - inget att avbryta"

    status_server.set_state(running=True)
    assert status_server.vanta_pa_kameran(timeout_s=0.1) is False, "vantan ger upp"
    assert pipeline.avbrott_begart() is True, "korningen ska bli ombedd att sluta"

    pipeline._AVBRYT.clear()  # noqa: SLF001 - stadar efter testet
    status_server.set_state(running=False)


def test_tidssida_lases_sakert_men_ar_inget_varde(
    server: StatusServer, monkeypatch: pytest.MonkeyPatch, kalibreringen: Path
) -> None:
    """Klockan och 02:00 har alla fyra siffrorna tända - de säger att rutorna
    sitter rätt, men de är inget värde (första positionen ska vara släckt)."""
    monkeypatch.setattr(status_server, "_ta_bilder", lambda *a, **kw: syntetiska_bilder("1336")[0])

    status, data = hamta(server, "/api/calibration/ny", {})

    assert status == 200
    lasning = data["lasning"]
    assert lasning["sida"] == "tid"
    assert lasning["ok"] is True, "siffrorna lases sakert"
    assert lasning["minsta_konfidens"] >= 0.5
    assert "tidssida" in lasning["text"]


def test_kalibreringspanelen_finns_i_sidan() -> None:
    """Panelen ska finnas i sidan - annars gar rutorna bara att flytta i koden."""
    sida = (Path(status_server.__file__).resolve().parent / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    for del_ in ('id="kalVy"', 'id="kalDuk"', 'id="kalRutor"', 'id="kalSpara"', 'id="kalMat"'):
        assert del_ in sida
    assert "/api/calibration" in sida
    # Panelerna ska ga att stanga, och vara stangda nar man kommer in.
    assert 'details class="panel"' in sida
    assert "<summary" in sida
