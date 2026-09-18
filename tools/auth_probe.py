"""Diagnostisera autentisering mot kameran.

Testar vilken auth-metod (Basic/Digest) kameran erbjuder och vilket märke/API
den exponerar. Skriver aldrig ut lösenordet.

Kör:  python tools/auth_probe.py
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IP = os.getenv("CAMERA_IP", "")
USER = os.getenv("CAMERA_USER", "")
PWD = os.getenv("CAMERA_PASSWORD", "")
PORT = os.getenv("CAMERA_HTTP_PORT", "80")
BASE = f"http://{IP}:{PORT}"


def show(label: str, r: requests.Response) -> None:
    auth_hdr = r.headers.get("WWW-Authenticate", "-")
    body = r.text.strip().replace("\n", " ")[:120]
    print(f"{label:<46} HTTP {r.status_code}  auth-hdr: {auth_hdr}")
    if r.status_code == 200:
        print(f"    svar: {body}")


def main() -> int:
    if not IP or not PWD:
        print("FEL: CAMERA_IP / CAMERA_PASSWORD saknas i .env")
        return 2

    print(f"Testar {BASE} som '{USER}' (losenord dolt)\n")

    # 1. Utan auth - vad erbjuder servern?
    for path in ("/ISAPI/System/deviceInfo", "/", "/onvif/device_service"):
        try:
            r = requests.get(BASE + path, timeout=8)
            show(f"Utan auth  {path}", r)
        except requests.RequestException as exc:
            print(f"Utan auth  {path}  FEL {exc}")

    print()

    # 2. Basic auth
    for path in ("/ISAPI/System/deviceInfo", "/onvif/device_service"):
        try:
            r = requests.get(BASE + path, auth=HTTPBasicAuth(USER, PWD), timeout=8)
            show(f"Basic auth {path}", r)
        except requests.RequestException as exc:
            print(f"Basic auth {path}  FEL {exc}")

    print()

    # 3. Digest auth
    for path in ("/ISAPI/System/deviceInfo", "/onvif/device_service"):
        try:
            r = requests.get(BASE + path, auth=HTTPDigestAuth(USER, PWD), timeout=8)
            show(f"Digest auth {path}", r)
        except requests.RequestException as exc:
            print(f"Digest auth {path}  FEL {exc}")

    print()

    # 4. Kanda tillverkarspecifika endpoints (Basic) for att identifiera marke
    probes = {
        "Dahua magicBox": "/cgi-bin/magicBox.cgi?action=getDeviceType",
        "Dahua currentTime": "/cgi-bin/global.cgi?action=getCurrentTime",
        "Hikvision ISAPI (Basic)": "/ISAPI/System/deviceInfo",
        "Axis (Basic)": "/axis-cgi/param.cgi?action=list&group=Brand",
        "Amcrest/ONVIF": "/onvif/media_service",
    }
    for label, path in probes.items():
        try:
            r = requests.get(BASE + path, auth=HTTPBasicAuth(USER, PWD), timeout=8)
            body = r.text.strip().replace("\n", " ")[:100]
            print(f"{label:<26} {path[:42]:<44} HTTP {r.status_code}  {body}")
        except requests.RequestException as exc:
            print(f"{label:<26} {path[:42]:<44} FEL {exc}")

    print()

    # 5. ONVIF SOAP GetDeviceInformation (Basic)
    soap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        "<s:Body>"
        '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
        "</s:Body></s:Envelope>"
    )
    try:
        r = requests.post(
            BASE + "/onvif/device_service",
            data=soap.encode(),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            auth=HTTPBasicAuth(USER, PWD),
            timeout=12,
        )
        print(f"ONVIF GetDeviceInformation  HTTP {r.status_code}")
        print(r.text.strip()[:900])
    except requests.RequestException as exc:
        print(f"ONVIF GetDeviceInformation  FEL {exc}")

    # 6. Tips: visa om losenordet innehaller tecken som ofta maste URL-kodas
    special = [ch for ch in PWD if ch in "@:/?#[]&% "]
    print(f"\nSpecialtecken i losenordet: {special if special else 'inga'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
