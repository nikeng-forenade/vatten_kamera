"""Konstanter for Home Assistant-integrationen.

Integrationen innehaller ingen bildbehandling alls: den fragar vardetjansten
(vattenkamera-tjansten, normalt en LXC i Proxmox) om det senaste vardet over
HTTP. Darfor behovs inga extra paket i Home Assistant - inte ens opencv.
"""

from __future__ import annotations

DOMAIN = "vatten_kamera"
NAME = "Vattenkamera"

# Var vardetjansten svarar. Porten ar STATUS_PORT i tjanstens .env.
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL = 60

CONF_HOST = "host"
CONF_PORT = "port"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ALLOW_RUN = "allow_run"

# Hur lange vi vantar pa svar per anrop.
REQUEST_TIMEOUT = 15
