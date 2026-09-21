"""Sammanfattar hur tjansten har gatt - for att se om den ar stabil.

Laser `history.jsonl` och `captures/runs/` och skriver ut:

    * hur manga lasningar som gjorts, och hur manga som gav ett varde
    * vardets utveckling (forsta, sista, lagsta, hogsta)
    * langsta luckan mellan tva lasningar - dar har tjansten statt still
    * om lasningarna kom med den takt som ar installld (EVERY_MINUTES)
    * konfidensens lage, och de korningar som misslyckades

    python tools/daily_report.py                # senaste dygnet
    python tools/daily_report.py --timmar 12    # senaste halva dygnet
    python tools/daily_report.py --alla         # allt som finns i historiken
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history
from config import CAPTURES_DIR, VERSION, load_config

RUNS_DIR = CAPTURES_DIR / "runs"


def _tid(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def sammanfatta(punkter: list[dict], *, takt_minuter: float) -> list[str]:
    """Rapporten som rader text."""
    if not punkter:
        return ["inga lasningar i perioden"]

    rader: list[str] = []
    med_varde = [p for p in punkter if isinstance(p.get("numeric"), (int, float))]
    utan = [p for p in punkter if not isinstance(p.get("numeric"), (int, float))]

    rader.append(f"lasningar:      {len(punkter)} ({len(med_varde)} med varde, {len(utan)} utan)")

    tider = [t for t in (_tid(p.get("read_at")) for p in punkter) if t is not None]
    if tider:
        forsta, sista = min(tider), max(tider)
        rader.append(
            f"period:         {forsta:%Y-%m-%d %H:%M} - {sista:%Y-%m-%d %H:%M}"
            f"  ({(sista - forsta).total_seconds() / 3600.0:.1f} h)"
        )

    if med_varde:
        varden = [float(p["numeric"]) for p in med_varde]
        rader.append(
            f"vardet:         {med_varde[0].get('display') or '?'} forst,"
            f" {med_varde[-1].get('display') or '?'} sist,"
            f" lagst {min(varden):.2f}, hogst {max(varden):.2f}"
        )

    if len(tider) > 1:
        tider.sort()
        luckor = [(b - a, a, b) for a, b in zip(tider, tider[1:])]
        storst, fran, till = max(luckor, key=lambda rad: rad[0])
        vantat = takt_minuter * 60.0 if takt_minuter > 0 else 90.0
        rader.append(
            f"langsta luckan: {storst.total_seconds() / 60.0:.1f} min"
            f"  ({fran:%H:%M} -> {till:%H:%M})"
        )
        if storst.total_seconds() > vantat * 3:
            rader.append("                ^ tjansten har statt still (eller last fel) har")
        omgangar = [rad[0].total_seconds() for rad in luckor]
        if omgangar:
            rader.append(
                f"mellan lasningar: median {sorted(omgangar)[len(omgangar) // 2] / 60.0:.1f} min"
                f" (installt {takt_minuter:.0f} min)"
            )

    if med_varde:
        konfidens = [float(p.get("confidence") or 0.0) for p in med_varde]
        rader.append(
            f"konfidens:      median {sorted(konfidens)[len(konfidens) // 2]:.2f},"
            f" lagst {min(konfidens):.2f}"
        )

    if utan:
        rader.append("utan varde:")
        for punkt in utan[-5:]:
            rader.append(f"  {punkt.get('read_at')}  {punkt.get('error') or 'okant fel'}")

    return rader


def main() -> int:
    parser = argparse.ArgumentParser(description="Sammanfattning av hur tjansten gatt")
    parser.add_argument("--timmar", type=float, default=24.0, help="hur langt bakåt (standard 24)")
    parser.add_argument("--alla", action="store_true", help="allt som finns i historiken")
    args = parser.parse_args()

    cfg = load_config()
    punkter = history.read(hours=None if args.alla else args.timmar)

    print(f"=== Vattenkamera {VERSION} ===")
    print(f"korningar pa disk: {len(list(RUNS_DIR.glob('*')))} st")
    print()
    for rad in sammanfatta(punkter, takt_minuter=cfg.run.every_minutes):
        print(rad)

    if punkter and not args.alla:
        sedan = datetime.now() - timedelta(hours=args.timmar)
        aldst = min((t for t in (_tid(p.get("read_at")) for p in punkter) if t), default=None)
        if aldst and aldst > sedan + timedelta(minutes=cfg.run.every_minutes * 3):
            print()
            print("obs: ingen lasning i borjan av perioden - tjansten startades senare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
