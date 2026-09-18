"""Kartlagger vad kameran exponerar via ISAPI (Hikvision).

Visar vilka installningar som finns och vilka som gar att andra - t.ex.
dags/nattlage, IR, ljusstyrka, kontrast och stromparametrar.

Kor:  python tools/isapi_probe.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IP = os.getenv("CAMERA_IP", "")
USER = os.getenv("CAMERA_USER", "")
PWD = os.getenv("CAMERA_PASSWORD", "")
PORT = os.getenv("CAMERA_HTTP_PORT", "80")
BASE = f"http://{IP}:{PORT}"

ENDPOINTS = [
    ("System/deviceInfo", "/ISAPI/System/deviceInfo"),
    ("System/capabilities", "/ISAPI/System/capabilities"),
    ("Image/channels/1", "/ISAPI/Image/channels/1"),
    ("Image/channels/1/color", "/ISAPI/Image/channels/1/color"),
    ("Streaming/channels", "/ISAPI/Streaming/channels"),
    ("Streaming/channels/101", "/ISAPI/Streaming/channels/101"),
    ("Streaming/channels/102", "/ISAPI/Streaming/channels/102"),
    ("Video/inputs/channels/1", "/ISAPI/System/Video/inputs/channels/1"),
    ("System/Network/interfaces/1", "/ISAPI/System/Network/interfaces/1"),
    ("System/time", "/ISAPI/System/time"),
]


def main() -> int:
    if not IP or not PWD:
        print("FEL: CAMERA_IP / CAMERA_PASSWORD saknas i .env")
        return 2

    session = requests.Session()
    session.auth = HTTPBasicAuth(USER, PWD)

    print(f"ISAPI-probe mot {BASE}\n")

    for label, path in ENDPOINTS:
        try:
            r = session.get(BASE + path, timeout=12)
        except requests.RequestException as exc:
            print(f"### {label}\n    FEL {exc}\n")
            continue

        print(f"### {label}  HTTP {r.status_code}")
        if r.status_code == 200:
            text = r.text.strip()
            if len(text) > 3000:
                text = text[:3000] + "\n...(trunkerad)"
            print(text)
        else:
            print(f"    {r.text.strip()[:200]}")
        print()

    # Snapshot-kvalitet: gar det styra bredd/hojd/komprimering?
    print("### Snapshot-parametrar")
    snap = BASE + "/ISAPI/Streaming/channels/101/picture"
    for params in (
        {},
        {"videoResolutionWidth": "2048", "videoResolutionHeight": "1536"},
        {"videoResolutionWidth": "1280", "videoResolutionHeight": "720"},
    ):
        try:
            r = session.get(snap, params=params, timeout=25)
            dims = "?"
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                import cv2
                import numpy as np

                img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    dims = f"{img.shape[1]}x{img.shape[0]}"
            print(f"    params={params or '{}'}  HTTP {r.status_code}  {len(r.content)//1024} kB  {dims}")
        except requests.RequestException as exc:
            print(f"    params={params}  FEL {exc}")

    # Dags/natt-lage och IR - leta efter relevanta taggar i bildinstallningarna
    print("\n### Relevant information ur bildinstallningar")
    try:
        r = session.get(BASE + "/ISAPI/Image/channels/1", timeout=12)
        if r.status_code == 200:
            for tag in ("brightnessLevel", "contrastLevel", "saturationLevel", "sharpnessLevel",
                        "DayNight", "IrCutFilter", "WDR", "mode", "enabled"):
                for m in re.finditer(rf"<{tag}>([^<]*)</{tag}>", r.text):
                    print(f"    {tag}: {m.group(1)}")
    except requests.RequestException as exc:
        print(f"    FEL {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
