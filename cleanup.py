"""Städar bort gamla bilder.

Varje körning sparar en katalog under `captures/runs/<datum>_<tid>/` med beviset
(bilden som värdet lästes ur) och — om `SAVE_FRAMES=true` — alla sidor från
körningen. Läser tjänsten hela tiden blir det många kataloger, så de äldsta
måste bort.

Två regler gör det här ofarligt:

* Bara kataloger **inuti** bildkatalogen rörs, och bara de som heter som en
  körning (`20260921_195427`). Allt annat lämnas i fred.
* **Den senaste körningen sparas alltid**, även om den är äldre än gränsen — då
  finns det alltid en bild till gränssnittet och Home Assistant.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config import CAPTURES_DIR

log = logging.getLogger("stadning")

RUNS_DIR = CAPTURES_DIR / "runs"

# Katalognamnen skrivs av pipeline: 20260921_195427
NAMN = re.compile(r"^\d{8}_\d{6}$")


@dataclass
class Resultat:
    """Vad städningen gjorde (eller skulle göra)."""

    borttagna: list[str]
    behallna: int
    mb: float
    dry: bool = False

    @property
    def antal(self) -> int:
        return len(self.borttagna)

    def text(self) -> str:
        if not self.borttagna:
            return f"inget att stada ({self.behallna} korningar kvar)"
        gjort = "skulle tas bort" if self.dry else "borttagna"
        return (
            f"{self.antal} korningar {gjort}, {self.mb:.1f} MB "
            f"({self.behallna} kvar)"
        )


def gamla_korningar(
    keep_days: int,
    *,
    runs_dir: Path = RUNS_DIR,
    nu: datetime | None = None,
) -> list[Path]:
    """Körningar äldre än gränsen — den senaste undantagen."""
    nu = nu or datetime.now()
    grans = nu - timedelta(days=max(1, keep_days))

    kandidater: list[tuple[datetime, Path]] = []
    for katalog in sorted(runs_dir.glob("*")):
        if not katalog.is_dir() or not NAMN.match(katalog.name):
            continue
        try:
            tid = datetime.strptime(katalog.name, "%Y%m%d_%H%M%S")
        except ValueError:
            continue  # omojligt datum i namnet - ror den inte
        kandidater.append((tid, katalog))

    if not kandidater:
        return []

    kandidater.sort(key=lambda par: par[0])
    senaste = kandidater[-1][1]
    return [katalog for tid, katalog in kandidater if tid < grans and katalog != senaste]


def stada(
    keep_days: int,
    *,
    runs_dir: Path = RUNS_DIR,
    dry: bool = False,
    nu: datetime | None = None,
) -> Resultat:
    """Tar bort körningar äldre än `keep_days` dygn."""
    alla = [k for k in runs_dir.glob("*") if k.is_dir() and NAMN.match(k.name)]
    offer = gamla_korningar(keep_days, runs_dir=runs_dir, nu=nu)

    storlek = 0
    borta: list[str] = []
    for katalog in offer:
        try:
            storlek += sum(f.stat().st_size for f in katalog.rglob("*") if f.is_file())
            if not dry:
                shutil.rmtree(katalog)
            borta.append(katalog.name)
        except OSError as exc:
            log.warning("kunde inte ta bort %s: %s", katalog, exc)

    resultat = Resultat(
        borttagna=borta,
        behallna=len(alla) - len(borta),
        mb=storlek / (1024 * 1024),
        dry=dry,
    )
    if resultat.antal:
        log.info("stadning (behall %d dygn): %s", keep_days, resultat.text())
    return resultat
