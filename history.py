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
