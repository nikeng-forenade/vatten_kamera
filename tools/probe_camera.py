"""Probe-kamera: verifierar inloggning, enhetsinfo och snapshot-uttag.

Kör:  python tools/probe_camera.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

# Hikvision DS-2CD2432F-IW kor Basic auth pa ISAPI (Digest ger 401).
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IP = os.getenv("CAMERA_IP", "")
USER = os.getenv("CAMERA_USER", "")
PWD = os.getenv("CAMERA_PASSWORD", "")
HTTP_PORT = os.getenv("CAMERA_HTTP_PORT", "80")
CHANNEL = os.getenv("CAMERA_CHANNEL", "101")

OUT = ROOT / "captures"
OUT.mkdir(exist_ok=True)


def base() -> str:
    return f"http://{IP}:{HTTP_PORT}"


def main() -> int:
    if not IP or not PWD:
        print("FEL: CAMERA_IP / CAMERA_PASSWORD saknas i .env")
        return 2

    auth = HTTPBasicAuth(USER, PWD)
    session = requests.Session()
    session.auth = auth

    print(f"Kamera : {base()}  (anvandare: {USER})")

    # 1. Enhetsinfo - bevisar att Basic-auth fungerar
    try:
        r = session.get(f"{base()}/ISAPI/System/deviceInfo", timeout=10)
    except requests.RequestException as exc:
        print(f"FEL: kunde inte na kameran: {exc}")
        return 3

    print(f"deviceInfo HTTP {r.status_code}")
    if r.status_code == 200:
        print(r.text.strip()[:800])
    elif r.status_code == 401:
        print("401 med Basic - testar Digest som fallback")
        session.auth = HTTPDigestAuth(USER, PWD)
        r = session.get(f"{base()}/ISAPI/System/deviceInfo", timeout=10)
        print(f"deviceInfo (digest) HTTP {r.status_code}")
        if r.status_code == 200:
            print(r.text.strip()[:800])
        else:
            print(r.text.strip()[:300])
    else:
        print(r.text.strip()[:300])

    # 2. Snapshot fran huvud- och sub-strom
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for ch, label in ((CHANNEL, "huvud"), ("102", "sub")):
        url = f"{base()}/ISAPI/Streaming/channels/{ch}/picture"
        try:
            s = session.get(url, timeout=25)
        except requests.RequestException as exc:
            print(f"snapshot {label:6} ({ch}): FEL {exc}")
            continue

        ctype = s.headers.get("Content-Type", "?")
        if s.status_code == 200 and s.content[:2] == b"\xff\xd8":
            path = OUT / f"probe_{label}_{stamp}.jpg"
            path.write_bytes(s.content)
            dims = ""
            try:
                import cv2
                import numpy as np

                img = cv2.imdecode(np.frombuffer(s.content, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    dims = f"  {img.shape[1]}x{img.shape[0]}px"
            except Exception:  # noqa: BLE001 - dims ar bara bonusinfo
                pass
            print(f"snapshot {label:6} ({ch}): {len(s.content):>8} bytes  {ctype}{dims}  -> {path.name}")
        else:
            print(f"snapshot {label:6} ({ch}): HTTP {s.status_code}  {ctype}  {s.content[:120]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
