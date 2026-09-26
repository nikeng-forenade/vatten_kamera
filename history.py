"""Historik: alla läsningar, så att värdet går att se över tid.

Varje läsning läggs till som en rad i `history.jsonl` (en JSON per rad). Filen
växer långsamt — en läsning var femte minut blir ~100 000 rader per år — men den
kapas ändå till `MAX_ROWS`, så att den aldrig kan växa utan gräns på en liten
maskin.

Raderna skrivs till en temp-fil som byts ut, precis som `latest.json`: en läsning
som sker mitt i en kontroll ska aldrig kunna se en halvskriven fil.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import HISTORY_FILE

log = logging.getLogger("historik")

# Hur många läsningar filen sparar. 5000 läsningar var femte minut är ~17 dygn;
# räcker gott för en graf, och filen blir några hundra kB.
MAX_ROWS = 5000


def _lines(path: Path) -> list[str]:
    """Filens rader, utan tomma rader."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("kunde inte lasa %s: %s", path, exc)
        return []
    return [line for line in text.splitlines() if line.strip()]


def _mal(path: Path | None) -> Path:
    """Vilken fil som avses.

    Las vid anropet och inte nar modulen laddas, sa att ett test (eller en annan
    DATA_DIR) kan byta fil utan att resten av programmet behover startas om.
    """
    return path if path is not None else HISTORY_FILE


def write(rows: list[dict[str, Any]], *, path: Path | None = None) -> None:
    """Skriver hela historiken."""
    path = _mal(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("kunde inte skriva %s: %s", path, exc)


def append(entry: dict[str, Any], *, path: Path | None = None) -> None:
    """Lägger till en läsning sist i historiken."""
    fil = _mal(path)
    rows = read(path=fil, limit=MAX_ROWS)
    rows.append(entry)
    write(rows[-MAX_ROWS:], path=fil)


def read(
    *,
    hours: float | None = None,
    limit: int = MAX_ROWS,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Läsningarna i tidsordning — äldst först.

    En trasig rad hoppas över i stället för att hela historiken ska försvinna.
    """
    rows: list[dict[str, Any]] = []
    for line in _lines(_mal(path)):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            rows.append(entry)

    rows.sort(key=lambda row: str(row.get("read_at") or ""))

    if hours is not None and hours > 0:
        cut = datetime.now() - timedelta(hours=hours)
        rows = [row for row in rows if (tid := _tid(row)) is not None and tid >= cut]

    if limit > 0:
        rows = rows[-limit:]
    return rows


def _tid(row: dict[str, Any]) -> datetime | None:
    """Tidpunkten för en läsning, utan tidszon (samma klocka som read_at)."""
    text = str(row.get("read_at") or "")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def flode_larm(
    *,
    troskel: float,
    minuter: float,
    path: Path | None = None,
    nu: datetime | None = None,
) -> dict[str, Any]:
    """Har flodet legat over troskeln i en obruten svit av lasningar sen `minuter`?

    Ett flode over troskeln en enstaka lasning ar normalt: pumpen kan kora en
    stund, och fyller man ett badkar rinner det i en kvart. Larmet kravs darfor av
    att *varje* lasning i en obruten svit ligger over troskeln och att sviten
    spanner over minst `minuter` - da tas det ut vatten hela tiden, och det ar
    vart att larma (lackage eller oppen ventil).

    Sviten mats i tid, inte i antal lasningar, sa att den fungerar oavsett hur
    ofta tjansten laser: 30 minuter ar tre lasningar var tionde minut eller
    femton varannan. En lasning utan kant flode bryter inte sviten - den sager
    ingenting om vattnet - men ett uppehall mellan lasningarna som ar langre an
    sjalva kravet (efter ett avbrott) gor att sviten inte gar att bedoma.
    """
    nu = nu or datetime.now()
    # Titta dubbelt sa langt bak som kravet: dels for att kunna skriva hur lange
    # det runnit ("minst X minuter"), dels for att sviten ska kunna bli langre an
    # kravet nar lasningarna kommer tatare an en gang per kvart.
    varden: list[tuple[datetime, float]] = []
    for rad in read(hours=max(1.0, minuter * 2) / 60.0, path=path):
        tid = _tid(rad)
        flode = rad.get("flow_numeric")
        if tid is None or not isinstance(flode, (int, float)):
            continue
        varden.append((tid, float(flode)))

    if not varden:
        return {"larm": False, "flode": None, "antal": 0, "text": "inget kant flode an"}

    nyast = varden[-1]
    if nyast[1] < troskel:
        return {
            "larm": False,
            "flode": nyast[1],
            "antal": 0,
            "text": f"flodet har varit under {troskel:g}",
        }
    gammal = (nu - nyast[0]).total_seconds() / 60.0
    if gammal > minuter:
        return {
            "larm": False,
            "flode": nyast[1],
            "antal": 0,
            "text": f"senaste lasningen ar {gammal:.0f} min gammal",
        }

    # Sviten: alla lasningar bakat sa lange de ligger over troskeln.
    svit = [nyast]
    for punkt in reversed(varden[:-1]):
        if punkt[1] < troskel:
            break
        svit.append(punkt)
    svit.reverse()

    lagst = min(flode for _, flode in svit)
    hogst = max(flode for _, flode in svit)
    stracka = (svit[-1][0] - svit[0][0]).total_seconds() / 60.0
    lucka = max(
        ((senare[0] - tidigare[0]).total_seconds() / 60.0 for tidigare, senare in zip(svit, svit[1:])),
        default=0.0,
    )
    svar = {"flode": nyast[1], "antal": len(svit), "minuter": round(stracka)}

    if len(svit) < 2:
        return {**svar, "larm": False, "text": "bara en lasning med kant flode an"}
    if stracka < minuter:
        return {
            **svar,
            "larm": False,
            "text": f"flodet har legat pa {lagst:.2f} i {stracka:.0f} min (kortare an {minuter:g})",
        }
    if lucka > minuter:
        return {
            **svar,
            "larm": False,
            "text": f"for glest mellan lasningarna ({lucka:.0f} min utan lasning)",
        }

    # Nar sviten anda bak i underlaget har den pagatt langre an vi kan se.
    minst = "minst " if svit[0] is varden[0] else ""
    return {
        **svar,
        "larm": True,
        "text": f"flodet har legat pa {lagst:.2f}-{hogst:.2f} i {minst}{stracka:.0f} min",
    }
