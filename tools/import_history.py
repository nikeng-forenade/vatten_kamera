"""Fyller historiken med de körningar som redan finns på disk.

Varje körning har en `summary.json` i `captures/runs/<tid>/`. Det här verktyget
läser dem och lägger in dem i `history.jsonl`, så att grafen visar det som redan
är läst — inte bara det som kommer efter att historiken började samlas.

    python tools/import_history.py            # alla sparade körningar
    python tools/import_history.py --dry      # visa bara vad som skulle skrivas

Dubbletter hoppas över (samma lästid), så verktyget kan köras om hur ofta som
helst.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history
from config import CAPTURES_DIR, load_config
from pipeline import RunSummary

RUNS_DIR = CAPTURES_DIR / "runs"


def las_summary(path: Path) -> RunSummary | None:
    """En sparad körning som RunSummary, eller None om filen inte går att läsa."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    kanda = {f.name for f in fields(RunSummary)}
    data = {key: value for key, value in raw.items() if key in kanda}
    try:
        return RunSummary(**data)
    except TypeError:
        # En gammal summary kan sakna falt som numera ar obligatoriska.
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fyll historiken fran sparade korningar")
    parser.add_argument("--dry", action="store_true", help="visa bara vad som skulle skrivas")
    parser.add_argument("--dir", type=Path, default=RUNS_DIR, help="katalog med korningar")
    args = parser.parse_args()

    cfg = load_config()
    enhet, decimaler = cfg.mqtt.unit, cfg.reader.decimals

    befintliga = history.read()
    sedda = {str(row.get("read_at") or "") for row in befintliga}

    nya: list[dict] = []
    for summary_path in sorted(args.dir.glob("*/summary.json")):
        summary = las_summary(summary_path)
        if summary is None:
            print(f"hoppar over {summary_path} (gick inte att lasa)")
            continue
        if summary.finished in sedda:
            continue
        sedda.add(summary.finished)
        nya.append(summary.to_status(unit=enhet, decimals=decimaler))

    if not nya:
        print(f"inget nytt att lagga till ({len(befintliga)} lasningar i historiken)")
        return 0

    for rad in nya:
        print(f"{rad['read_at']}  {rad['display'] or '-':>6}  konfidens {rad['confidence']}")

    if args.dry:
        print(f"\n{len(nya)} lasningar skulle laggas till (torrkorning, inget skrivet)")
        return 0

    history.write(befintliga + nya)
    print(f"\n{len(nya)} lasningar inlagda - historiken har nu {len(befintliga) + len(nya)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
