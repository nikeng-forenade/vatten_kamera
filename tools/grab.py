"""Sparar en serie kamerabilder pa disk sa att efteranalysen kan goras offline.

Displayen vaxlar mellan olika vyer, sa en serie bilder behovs for att se alla.
Att spara dem forst gor att analysen kan koras om och om igen utan att kameran
behover vara igang.

Kor:
    python tools/grab.py --frames 20 --out captures/session
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera import CameraError, HikvisionCamera  # noqa: E402
from config import ROOT, load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", type=Path, help="katalog (default: captures/session)")
    args = parser.parse_args()

    out_dir = args.out or (ROOT / "captures" / "session")
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    camera = HikvisionCamera(cfg.camera)
    saved = 0
    try:
        for index in range(args.frames):
            frame = camera.snapshot()
            name = f"{index:03d}_{datetime.now():%H%M%S}.jpg"
            (out_dir / name).write_bytes(frame.jpeg)
            saved += 1
            print(f"{name}  {frame.width}x{frame.height}  {len(frame.jpeg) // 1024} kB")
            if index < args.frames - 1:
                time.sleep(args.interval)
    except CameraError as exc:
        print(f"FEL: {exc}")
        return 1
    finally:
        camera.close()

    print(f"\n{saved} bilder -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
