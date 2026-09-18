"""Följer displayen live och skriver ut den som ASCII-konst i terminalen.

Skriver bara ut när bilden förändras, så en lång körning ger en kompakt
tidslinje över vilka värden displayen växlar mellan.

Exempel:
    python tools/watch_display.py --roi 430,1185,590,1265 --minutes 2 --interval 2
    python tools/watch_display.py --roi 430,1185,590,1265 --minutes 5 --ascii-width 90
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

RAMP = " .:-=+*#%@"


def to_ascii(gray: np.ndarray, width: int, height: int) -> str:
    small = cv2.resize(gray, (width, height), interpolation=cv2.INTER_AREA)
    lo, hi = float(small.min()), float(small.max())
    span = max(1.0, hi - lo)
    norm = (small.astype(np.float32) - lo) / span
    idx = np.clip((norm * (len(RAMP) - 1)).round().astype(int), 0, len(RAMP) - 1)
    return "\n".join("".join(RAMP[i] for i in row) for row in idx)


def signature(gray: np.ndarray) -> str:
    """Grov signatur: trosklad, nedskalad bild -> hash. Liknande varden ger liknande hash."""
    small = cv2.resize(gray, (48, 14), interpolation=cv2.INTER_AREA)
    _, th = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, buf = cv2.imencode(".png", th)
    return hashlib.sha1(buf.tobytes()).hexdigest()[:10]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--roi", required=True, help="x1,y1,x2,y2")
    p.add_argument("--minutes", type=float, default=2.0)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--ascii-width", type=int, default=72)
    p.add_argument("--ascii-height", type=int, default=10)
    p.add_argument("--threshold", type=int, help="fast troskel (0-255) i stallet for Otsu")
    p.add_argument("--clip", type=float, default=0.0,
                   help="klipp bort denna andel av ROI:ns hojd nedtill (t.ex. 0.3 for att slippa reflektionen under siffrorna)")
    args = p.parse_args()

    ip = os.getenv("CAMERA_IP", "")
    user = os.getenv("CAMERA_USER", "")
    pwd = os.getenv("CAMERA_PASSWORD", "")
    port = os.getenv("CAMERA_HTTP_PORT", "80")
    ch = os.getenv("CAMERA_CHANNEL", "101")
    if not ip or not pwd:
        print("FEL: CAMERA_IP / CAMERA_PASSWORD saknas i .env")
        return 2

    x1, y1, x2, y2 = (int(v) for v in args.roi.split(","))
    url = f"http://{ip}:{port}/ISAPI/Streaming/channels/{ch}/picture"
    session = requests.Session()
    session.auth = HTTPBasicAuth(user, pwd)

    deadline = time.time() + args.minutes * 60
    last_sig = ""
    frames = 0
    errors = 0

    print(f"Foljer displayen i {args.minutes} min, bild var {args.interval}s. ROI={args.roi}")
    print("Skriver bara ut nar vardet forandras.\n")

    while time.time() < deadline:
        t0 = time.time()
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException as exc:
            errors += 1
            print(f"{datetime.now():%H:%M:%S}  FEL {exc}")
            time.sleep(args.interval)
            continue

        frames += 1
        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                sub = img[y1:y2, x1:x2]
                if args.clip > 0:
                    cut = int(sub.shape[0] * args.clip)
                    sub = sub[: sub.shape[0] - cut, :]
                # forstora sa siffrorna blir lasbara aven i ASCII
                big = cv2.resize(sub, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
                blur = cv2.GaussianBlur(big, (5, 5), 0)
                if args.threshold:
                    _, th = cv2.threshold(blur, args.threshold, 255, cv2.THRESH_BINARY)
                else:
                    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

                sig = signature(th)
                if sig != last_sig:
                    art = to_ascii(th, args.ascii_width, args.ascii_height)
                    print(f"--- {datetime.now():%H:%M:%S}  sig {sig}  " + "-" * 20)
                    print(art)
                    print()
                    last_sig = sig
        else:
            errors += 1
            print(f"{datetime.now():%H:%M:%S}  HTTP {r.status_code}")

        time.sleep(max(0.0, args.interval - (time.time() - t0)))

    print(f"Klart: {frames} bilder, {errors} fel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
