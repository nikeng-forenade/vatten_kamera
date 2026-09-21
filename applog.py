"""Loggning for bade kommandoraden och webbgranssnittet.

Loggen gar till terminalen och till en fil i datakatalogen. Filen ar den
granssnittet visar de sista raderna ur, sa att en korning gar att folja i
efterhand - aven nar tjansten kor som en systemtjanst utan terminal.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import LOG_FILE

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(verbose: bool = False, *, to_file: bool = True) -> list[logging.Handler]:
    """Satter upp loggning. Returnerar handtaggarna som anvands."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if to_file:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            )
        except OSError as exc:  # utan skrivratt blir det bara terminalen
            logging.getLogger(__name__).warning("kunde inte logga till %s: %s", LOG_FILE, exc)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=FORMAT,
        handlers=handlers,
        force=True,
    )
    return handlers
