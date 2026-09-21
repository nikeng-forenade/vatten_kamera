#!/usr/bin/env bash
# Vattenkamera - uppdatera en LXC som redan kor.
#
# Fran Proxmox-hostens skal (byt ut 210 mot ditt container-ID):
#   pct exec 210 -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/nikeng-forenade/vatten_kamera/main/lxc/update.sh)"
#
# Eller inuti containern:
#   bash <(curl -fsSL https://raw.githubusercontent.com/nikeng-forenade/vatten_kamera/main/lxc/update.sh)
#
# .env och data/ (kalibrering, bilder, senaste vardet) ror inte uppdateringen.

set -e

APP_DIR="/opt/vattenkamera"
SERVICE="vatten-kamera"

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "FEL: $APP_DIR ser inte ut som en installation (ingen git-katalog)."
  echo "     Kor install.sh forst."
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "FEL: kor som root."
  exit 1
fi

cd "$APP_DIR"

FORRA=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
echo "=== Vattenkamera - uppdatering ==="
echo "Nuvarande version: $FORRA"
echo ""

git fetch --quiet origin main
NY=$(git rev-parse --short origin/main)
if [[ "$FORRA" == "$NY" ]]; then
  echo "Inget nytt att hamta - startar tjansten anda, sa att eventuella"
  echo "installningsandringar slar igenom."
else
  echo "Uppdaterar $FORRA -> $NY"
  git reset --hard --quiet "origin/main"
fi

# Beroenden kan ha andrats.
echo "Kontrollerar beroenden..."
grep -v '^opencv-python' requirements.txt > /tmp/requirements.txt
"$APP_DIR/.venv/bin/pip" install --quiet -r /tmp/requirements.txt
"$APP_DIR/.venv/bin/pip" install --quiet "opencv-python-headless>=4.9"

echo "Startar om tjansten..."
systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  echo ""
  echo "Klart. Kor nu:"
  echo "  git -C $APP_DIR log --oneline -1"
  echo "  journalctl -u $SERVICE -f"
else
  echo "Tjansten startade inte - sista loggen:"
  journalctl -u "$SERVICE" -n 20 --no-pager || true
  exit 1
fi
