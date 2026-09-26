"""Flyttar installationens installningar och kalibrering till en annan maskin.

Allt som ar inmatt och justerat - `.env`, kalibreringen, lasprofilen och kamerans
backup - samlas i en fil som kan kopieras till en annan maskin och lasas in dar.
Det ar sa en LXC far samma installningar som den maskin dar lasningen provades ut.

**Losenord och tokens foljer aldrig med.** De lamnas utanfor filen och fylls i pa
den nya maskinen (i granssnittet, eller i `.env` for hand).

Kor:
    python tools/settings_file.py --spara                # -> settings_export.json
    python tools/settings_file.py --las settings_export.json
    python tools/settings_file.py --las settings_export.json --torr   # visa bara

Filnamnet `settings_export.json` ar gitignorerat: den innehaller kamerans adress
och ska stanna pa dina egna maskiner.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Verktyget ligger i tools/ - projektets moduler ligger ett steg upp.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import settings_store  # noqa: E402
from camera_settings import BACKUP_FILE, PROFILE_FILE  # noqa: E402

log = logging.getLogger("installningar")

STANDARD_FIL = "settings_export.json"

# Poster i .env som ar hemliga. Allt som slutar pa _PASSWORD eller _TOKEN
# filtreras dessutom bort, sa att en ny hemlighet inte kan smita med av misstag.
HEMLIGHETER = ("CAMERA_PASSWORD", "HA_TOKEN", "MQTT_PASSWORD")

# Var installationen bor - inte hur den laser. Att flytta dem till en annan maskin
# skulle peka tjansten fel, sa de foljer varken med ut eller in.
MASKINNYCKEL = (
    "DATA_DIR",
    "CAPTURES_DIR",
    "CALIBRATION_FILE",
    "LATEST_FILE",
    "HISTORY_FILE",
    "LOG_FILE",
)


def _ar_maskinnyckel(nyckel: str) -> bool:
    return nyckel.upper() in MASKINNYCKEL


def _ar_hemlig(nyckel: str) -> bool:
    namn = nyckel.upper()
    return namn in HEMLIGHETER or namn.endswith(("_PASSWORD", "_TOKEN"))


def _las_json(path: Path) -> dict | None:
    """Filens innehall, eller None om den inte finns eller inte gar att lasa."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("kunde inte lasa %s: %s", path, exc)
        return None


def _skriv_json(path: Path, data: Any, *, kopia: bool = True) -> str:
    """Skriver filen och behaller den gamla som .bak. Returnerar vad som hande."""
    ny = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        gammal = path.read_text(encoding="utf-8")
        if gammal == ny:
            return "oforandrad"
        if kopia:
            path.with_name(path.name + ".bak").write_text(gammal, encoding="utf-8")
    path.write_text(ny, encoding="utf-8")
    return "skriven"


# ---------------------------------------------------------------------------
# Samla ihop (kor pa maskinen som har installningarna)
# ---------------------------------------------------------------------------


def samla(
    env: Path | None = None,
    kalibrering: Path | None = None,
    profil: Path | None = None,
    backup: Path | None = None,
) -> dict[str, Any]:
    """Allt lokalt som hor till installationen - utan hemligheter."""
    env = env or settings_store.ENV_FILE
    kalibrering = kalibrering or config.CALIBRATION_FILE
    profil = profil or PROFILE_FILE
    backup = backup or BACKUP_FILE

    varden = settings_store.read_env(env)
    utelamnade = sorted(nyckel for nyckel in varden if _ar_hemlig(nyckel))

    data: dict[str, Any] = {
        "version": config.VERSION,
        "skapad": datetime.now().isoformat(timespec="seconds"),
        "maskin": socket.gethostname(),
        "hemligheter_utelamnade": utelamnade,
        "env": {
            nyckel: varde
            for nyckel, varde in sorted(varden.items())
            if not _ar_hemlig(nyckel) and not _ar_maskinnyckel(nyckel)
        },
    }

    for nyckel, fil in (
        ("kalibrering", kalibrering),
        ("lasprofil", profil),
        ("kamerabackup", backup),
    ):
        inne = _las_json(fil)
        if inne is not None:
            data[nyckel] = inne
    return data


# ---------------------------------------------------------------------------
# Lagg in (kor pa maskinen som ska fa installningarna)
# ---------------------------------------------------------------------------


def skriv(
    data: dict[str, Any],
    env: Path | None = None,
    kalibrering: Path | None = None,
    profil: Path | None = None,
    backup: Path | None = None,
    *,
    torr: bool = False,
) -> dict[str, Any]:
    """Lagger in filens varden och skriver filerna. Returnerar vad som gjordes.

    `torr=True` visar bara vad som skulle handa - ingenting skrivs.
    """
    env = env or settings_store.ENV_FILE
    kalibrering = kalibrering or config.CALIBRATION_FILE
    profil = profil or PROFILE_FILE
    backup = backup or BACKUP_FILE

    varden = {
        nyckel: str(varde)
        for nyckel, varde in (data.get("env") or {}).items()
        if not _ar_hemlig(nyckel) and not _ar_maskinnyckel(nyckel)
    }
    hemligheter = sorted(
        nyckel for nyckel in (data.get("env") or {}) if _ar_hemlig(nyckel)
    )
    maskinnycklar = sorted(
        nyckel for nyckel in (data.get("env") or {}) if _ar_maskinnyckel(nyckel)
    )
    kanda = {nyckel: varde for nyckel, varde in varden.items() if nyckel in settings_store.BY_KEY}
    okanda = {nyckel: varde for nyckel, varde in varden.items() if nyckel not in settings_store.BY_KEY}

    rapport: dict[str, Any] = {
        "env": sorted(kanda),
        "okanda": sorted(okanda),
        "hemligheter": hemligheter,
        "maskinnycklar": maskinnycklar,
        "problem": {},
        "filer": {},
        "torr": torr,
    }

    if torr:
        nuvarande = settings_store.read_env(env)
        rapport["andrade"] = sorted(
            nyckel for nyckel, varde in kanda.items() if nuvarande.get(nyckel, "") != varde
        )
        rapport["filer"] = {
            nyckel: ("skulle skrivas" if data.get(nyckel) else "saknas i filen")
            for nyckel in ("kalibrering", "lasprofil", "kamerabackup")
        }
        return rapport

    andrade, problem = settings_store.apply_changes(kanda, env)
    rapport["andrade"] = sorted(andrade)
    rapport["problem"] = problem

    if okanda:
        settings_store.write_env(settings_store.read_env(env), okanda, env)

    for nyckel, innehall, mal in (
        ("kalibrering", data.get("kalibrering"), kalibrering),
        ("lasprofil", data.get("lasprofil"), profil),
        ("kamerabackup", data.get("kamerabackup"), backup),
    ):
        if innehall is None:
            rapport["filer"][nyckel] = "saknas i filen"
            continue
        rapport["filer"][nyckel] = _skriv_json(mal, innehall)

    return rapport


def _visa(rapport: dict[str, Any]) -> None:
    """Skriver ut vad som hande - och vad som aterstar att gora."""
    if rapport.get("torr"):
        print("Torrkorning - ingenting skrivs.")
    if rapport.get("andrade"):
        print(f"installningar andrade : {', '.join(rapport['andrade'])}")
    if rapport.get("okanda"):
        print(f"ovriga nycklar i .env : {', '.join(rapport['okanda'])}")
    for nyckel, vad in (rapport.get("filer") or {}).items():
        print(f"{nyckel:<14}: {vad}")
    if rapport.get("problem"):
        print("hoppade over (gick inte att anvanda):")
        for nyckel, fel in rapport["problem"].items():
            print(f"  {nyckel}: {fel}")
    if rapport.get("hemligheter"):
        print(
            "hemligheter i filen togs inte med: " + ", ".join(rapport["hemligheter"])
        )
    if rapport.get("maskinnycklar"):
        print(
            "maskinspecifika sokvagar hoppade over: " + ", ".join(rapport["maskinnycklar"])
        )
    print(
        "losenord och tokens foljer inte med - fyll i kamerans losenord i granssnittet "
        "(Installningar -> Kameran) och starta om tjansten om nagot kravde det."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flytta installningar och kalibrering mellan maskiner (utan losenord)."
    )
    parser.add_argument(
        "--spara",
        nargs="?",
        const=STANDARD_FIL,
        metavar="FIL",
        help=f"samla installningarna i en fil (standard: {STANDARD_FIL})",
    )
    parser.add_argument("--las", metavar="FIL", help="lagg in installningarna ur en fil")
    parser.add_argument("--torr", action="store_true", help="visa bara vad som skulle goras")
    args = parser.parse_args()

    if bool(args.spara) == bool(args.las):
        parser.error("valj --spara eller --las")

    if args.spara:
        fil = Path(args.spara)
        data = samla()
        fil.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"installningarna samlade i {fil}")
        print(f"  .env-nycklar   : {len(data['env'])}")
        print(f"  hemligheter    : {', '.join(data['hemligheter_utelamnade']) or 'inga'}")
        for nyckel in ("kalibrering", "lasprofil", "kamerabackup"):
            print(f"  {nyckel:<15}: {'med' if nyckel in data else 'saknas'}")
        print("")
        print("Kopiera filen till den andra maskinen och kor dar:")
        print(f"  python tools/settings_file.py --las {fil.name}")
        return 0

    fil = Path(args.las)
    if not fil.exists():
        print(f"FEL: hittar inte {fil}")
        return 1
    try:
        data = json.loads(fil.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"FEL: {fil} ar inte giltig JSON: {exc}")
        return 1

    rapport = skriv(data, torr=args.torr)
    _visa(rapport)
    return 0


if __name__ == "__main__":
    from applog import setup_logging

    setup_logging(False)
    raise SystemExit(main())
