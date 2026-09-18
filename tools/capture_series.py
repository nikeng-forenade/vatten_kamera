"""Tar en serie snapshots och mater hur displayomradet forandras over tid.

Anvands for att forsta displayens beteende (klocka? vaxlande varden? stilla?)
och for att samla bilder att bygga OCR/troskling mot.

Exempel:
    python tools/capture_series.py --count 8 --interval 3
    python tools/capture_series.py --count 10 --interval 2 --roi 440,1200,580,1255
"""

from __future__ import annotations

import argparse
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


def parse_roi(text: str | None) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [int(p) for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError("--roi ska vara x1,y1,x2,y2")
    return parts[0], parts[1], parts[2], parts[3]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=6, help="antal bilder")
    p.add_argument("--interval", type=float, default=3.0, help="sekunder mellan bilder")
    p.add_argument("--roi", type=str, help="x1,y1,x2,y2 for att mata andring i displayen")
    p.add_argument("--outdir", type=Path, default=ROOT / "captures" / "series")
    args = p.parse_args()

    ip = os.getenv("CAMERA_IP", "")
    user = os.getenv("CAMERA_USER", "")
    pwd = os.getenv("CAMERA_PASSWORD", "")
    port = os.getenv("CAMERA_HTTP_PORT", "80")
    ch = os.getenv("CAMERA_CHANNEL", "101")
    if not ip or not pwd:
        print("FEL: CAMERA_IP / CAMERA_PASSWORD saknas i .env")
        return 2

    roi = parse_roi(args.roi)
    url = f"http://{ip}:{port}/ISAPI/Streaming/channels/{ch}/picture"
    session = requests.Session()
    session.auth = HTTPBasicAuth(user, pwd)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = args.outdir / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    prev_roi_img: np.ndarray | None = None
    print(f"Tar {args.count} bilder, {args.interval}s mellanrum -> {outdir}")
    if roi:
        print(f"ROI for andringsmatt: {roi}")

    for i in range(1, args.count + 1):
        t0 = time.time()
        try:
            r = session.get(url, timeout=25)
        except requests.RequestException as exc:
            print(f"[{i}] FEL {exc}")
            time.sleep(args.interval)
            continue

        if r.status_code != 200 or r.content[:2] != b"\xff\xd8":
            print(f"[{i}] HTTP {r.status_code}, ingen JPEG")
            time.sleep(args.interval)
            continue

        now = datetime.now()
        path = outdir / f"{now.strftime('%H%M%S')}_{i:02d}.jpg"
        path.write_bytes(r.content)

        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        info = f"[{i}] {now.strftime('%H:%M:%S')}  {len(r.content)//1024} kB"

        if roi and img is not None:
            x1, y1, x2, y2 = roi
            sub = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            bright = float(sub.max())
            # hur mycket lyser displayen? (max-pixel = de tanda segmenten)
            info += f"  max-pixel i ROI: {bright:5.1f}"
            if prev_roi_img is not None and prev_roi_img.shape == sub.shape:
                diff = float(np.mean(np.abs(sub.astype(np.int16) - prev_roi_img.astype(np.int16))))
                info += f"  andring: {diff:5.1f}"
            prev_roi_img = sub

        print(info)

        if i < args.count:
            time.sleep(max(0.0, args.interval - (time.time() - t0)))

    print(f"Klart -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
