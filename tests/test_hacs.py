"""Tester for HACS-integrationen och dess filer.

Integrationen kors inne i Home Assistant, sa sjalva koden provas i HA:s egen
testmiljo. Har kontrolleras allt som kan kontrolleras utan HA: att manifest,
hacs.json och oversattningarna ar giltiga och att nycklarna i oversattningarna
matchar nycklarna i koden. Det ar just den sortens sak som annars smyger sig
isär - en ny entitet utan namn i granssnittet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from config import VERSION

ROT = Path(__file__).resolve().parent.parent
INTEGRATION = ROT / "custom_components" / "vatten_kamera"
OVERSATTNINGAR = INTEGRATION / "translations"


def las_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sokvagar(data: dict, prefix: str = "") -> set[str]:
    """Alla nyckelvagar i en JSON-struktur, sa att tva sprak kan jamforas."""
    vagar: set[str] = set()
    for nyckel, varde in data.items():
        vagen = f"{prefix}.{nyckel}" if prefix else nyckel
        vagar.add(vagen)
        if isinstance(varde, dict):
            vagar |= _sokvagar(varde, vagen)
    return vagar


# --- Filerna --------------------------------------------------------------


def test_integrationen_har_alla_filer() -> None:
    for namn in (
        "__init__.py",
        "api.py",
        "binary_sensor.py",
        "button.py",
        "camera.py",
        "config_flow.py",
        "const.py",
        "coordinator.py",
        "entity.py",
        "manifest.json",
        "sensor.py",
        "strings.json",
    ):
        assert (INTEGRATION / namn).exists(), f"{namn} saknas"


def test_manifest_ar_giltigt() -> None:
    manifest = las_json(INTEGRATION / "manifest.json")

    assert manifest["domain"] == INTEGRATION.name
    assert manifest["config_flow"] is True
    # Versionen ska folja tjansten, sa att det syns vad som kor var.
    assert manifest["version"] == VERSION
    # Ingen bildbehandling i HA: allt tungt ligger i tjansten.
    assert manifest["requirements"] == []
    for nyckel in ("name", "codeowners", "documentation", "issue_tracker", "iot_class"):
        assert manifest.get(nyckel), f"{nyckel} saknas i manifest.json"


def test_hacs_json_ar_giltig() -> None:
    hacs = las_json(ROT / "hacs.json")

    assert hacs["name"]
    assert hacs["content_in_root"] is False  # integrationen ligger i custom_components/


def test_brand_ikonen_finns() -> None:
    """HA och HACS visar ikonen ur brand/ - den ska vara kvadratisk och 256x256.

    Utan den ritar Home Assistant en tom ruta i stället för integrationsikonen.
    """
    import cv2

    fil = INTEGRATION / "brand" / "icon.png"
    assert fil.exists(), "brand/icon.png saknas (kor tools/make_icon.py)"

    bild = cv2.imread(str(fil), cv2.IMREAD_UNCHANGED)
    assert bild is not None, "icon.png gick inte att lasa"
    assert bild.shape[0] == bild.shape[1] == 256, f"fel storlek: {bild.shape[:2]}"
    assert bild.shape[2] == 4, "ikonen ska ha genomskinliga horn"


def test_integrationen_oppnar_inga_nya_paket() -> None:
    """HACS ska kunna installera utan att dra in nagot extra.

    All bildbehandling ligger i tjansten - integrationen fragar bara om vardet.
    Darfor far den inte importera cv2, numpy eller nagot annat tungt.
    """
    forbjudna = {"cv2", "numpy", "opencv", "PIL", "pytesseract"}
    for fil in INTEGRATION.glob("*.py"):
        for rad in fil.read_text(encoding="utf-8").splitlines():
            if not rad.startswith(("import ", "from ")):
                continue
            modul = rad.split()[1].split(".")[0]
            assert modul not in forbjudna, f"{fil.name} importerar {modul}"


def test_inga_hemligheter_i_filerna() -> None:
    """Ingen token eller losenord ska hamna i integrationen."""
    for fil in list(INTEGRATION.glob("*.py")) + [INTEGRATION / "manifest.json"]:
        text = fil.read_text(encoding="utf-8").lower()
        for ord_ in ("password=", "token=", "bearer "):
            assert ord_ not in text, f"{fil.name} verkar innehalla {ord_}"


# --- Oversattningarna -----------------------------------------------------


def test_oversattningarna_har_samma_nycklar_som_strings() -> None:
    strings = las_json(INTEGRATION / "strings.json")

    for sprak in ("sv", "en"):
        fil = OVERSATTNINGAR / f"{sprak}.json"
        assert fil.exists(), f"{sprak}.json saknas"
        assert _sokvagar(las_json(fil)) == _sokvagar(strings), f"{sprak}.json avviker"


def test_alla_entiteter_har_ett_namn() -> None:
    """Varje _attr_translation_key i koden maste finnas i oversattningen."""
    strings = las_json(INTEGRATION / "strings.json")

    for filnamn, gren in (
        ("sensor.py", "sensor"),
        ("binary_sensor.py", "binary_sensor"),
        ("button.py", "button"),
        ("camera.py", "camera"),
    ):
        kalla = (INTEGRATION / filnamn).read_text(encoding="utf-8")
        nycklar = set(re.findall(r'_attr_translation_key = "([^"]+)"', kalla))
        assert nycklar, f"inga entiteter hittades i {filnamn}"
        for nyckel in nycklar:
            assert nyckel in strings["entity"][gren], f"{gren}.{nyckel} saknas i strings.json"
            for sprak in ("sv", "en"):
                oversattning = las_json(OVERSATTNINGAR / f"{sprak}.json")
                assert oversattning["entity"][gren][nyckel].get("name")


def test_statuslagena_stammer_med_oversattningen() -> None:
    kalla = (INTEGRATION / "sensor.py").read_text(encoding="utf-8")
    traff = re.search(r"STATUS_LAGEN = \(([^)]*)\)", kalla)
    assert traff is not None

    i_koden = set(re.findall(r'"([^"]+)"', traff.group(1)))
    i_oversattningen = set(las_json(INTEGRATION / "strings.json")["entity"]["sensor"]["status"]["state"])
    assert i_koden == i_oversattningen


def test_konfigflodet_har_svenska_feltexter() -> None:
    svenska = las_json(OVERSATTNINGAR / "sv.json")
    kalla = (INTEGRATION / "config_flow.py").read_text(encoding="utf-8")

    anvanda = set(re.findall(r'errors\["base"\] = "([^"]+)"', kalla))
    assert anvanda
    for nyckel in anvanda:
        assert nyckel in svenska["config"]["error"], f"{nyckel} saknas i sv.json"
