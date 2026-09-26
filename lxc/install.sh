#!/usr/bin/env bash
# Vattenkamera - installation i en Debian/Ubuntu-LXC.
#
# Kors inuti containern, som root:
#   bash install.sh
#   bash install.sh --camera-ip <kamerans adress> --camera-password hemligt
#
# Skriptet ar gjort for att kunna koras om: koden uppdateras, .env och data
# lamnas i fred.

set -e

APP_DIR="/opt/vattenkamera"
DATA_DIR="${APP_DIR}/data"
REPO="https://github.com/nikeng-forenade/vatten_kamera.git"
SERVICE="vatten-kamera"

RUN_AT="02:05:00"
MODE="intervall"
EVERY_MINUTES="5"
CAMERA_IP=""
CAMERA_USER=""
CAMERA_PASSWORD=""
HA_URL=""
HA_TOKEN=""
UNIT=""
STATUS_PORT="8099"
CALIBRATION=""
TIMEZONE=""

usage() {
  cat <<'TEXT'
Anvandning: bash install.sh [flaggor]

  --run-at HH:MM:SS        Klockslaget da vardet visas (DATORNS tid - pumpens klocka gar efter)
  --mode intervall|manuell|natt   Nar den ska lasa (standard intervall)
  --every-minutes X        Hur ofta i lage intervall (0 = hela tiden, standard 5)
  --camera-ip IP           Kamerans adress (kan ocksa fyllas i i granssnittet)
  --camera-user NAMN       Kamerans anvandare (t.ex. admin)
  --camera-password LOSEN  Kamerans losenord
  --ha-url URL             Home Assistant-adress
  --ha-token TOKEN         Langlivad token
  --unit ENHET             Enhet vid sensorn, t.ex. l
  --status-port PORT       Port for granssnittet (standard 8099)
  --calibration FIL        Lagg in en fardig calibration.json
  --timezone TZ            Tidszon, t.ex. Europe/Stockholm (annars UTC i containern)
  --help                   Visa det har
TEXT
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-at) RUN_AT="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --every-minutes) EVERY_MINUTES="$2"; shift 2 ;;
    --camera-ip) CAMERA_IP="$2"; shift 2 ;;
    --camera-user) CAMERA_USER="$2"; shift 2 ;;
    --camera-password) CAMERA_PASSWORD="$2"; shift 2 ;;
    --ha-url) HA_URL="$2"; shift 2 ;;
    --ha-token) HA_TOKEN="$2"; shift 2 ;;
    --unit) UNIT="$2"; shift 2 ;;
    --status-port) STATUS_PORT="$2"; shift 2 ;;
    --calibration) CALIBRATION="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --help|-h) usage ;;
    *) echo "Okand flagga: $1"; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "FEL: kor som root (det gor du som standard i en LXC)."
  exit 1
fi

echo "=== Vattenkamera - installation ==="
echo "Lage:        $MODE"
case "$MODE" in
  intervall)
    if [[ "$EVERY_MINUTES" == "0" ]]; then
      echo "Laser:       hela tiden (nasta lasning strax efter den forra)"
    else
      echo "Laser:       var $EVERY_MINUTES minut"
    fi
    ;;
  manuell) echo "Laser:       bara nar du trycker 'Las nu'" ;;
  natt)    echo "Laser:       en gang per dygn strax innan $RUN_AT (datorns tid)" ;;
esac
if [[ -n "$CAMERA_IP" ]]; then
  echo "Kamera:      $CAMERA_IP (anvandare ${CAMERA_USER:-tom - fylls i i granssnittet})"
else
  echo "Kamera:      fylls i i granssnittet efterat"
fi
echo "Granssnitt:  port $STATUS_PORT"
if [[ -n "$TIMEZONE" ]]; then
  echo "Tidszon:     $TIMEZONE"
fi
echo ""
echo "Det har tar nagra minuter (Python och OpenCV ska in) och ser nastan stilla ut"
echo "medan paketen installeras - avbryt inte."
echo ""

# --- Paket -----------------------------------------------------------------
# Skalet som startade installationen har ofta ett LANG som inte finns i
# containern; det ger locale- och perl-varningar som ser ut som fel men inte ar det.
export LC_ALL=C
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl \
  libglib2.0-0 libgomp1 >/dev/null

# --- Tidszonen -------------------------------------------------------------
# En container har sin egen tidszon och arver inte hostens: star den i UTC visar
# granssnittet och historiken fel klockslag, och en nattkorning skulle starta
# tva timmar fel (vardet visas bara nagra sekunder strax efter 02:00).
if [[ -n "$TIMEZONE" ]] && command -v timedatectl >/dev/null 2>&1; then
  if timedatectl set-timezone "$TIMEZONE" 2>/dev/null; then
    echo "Tidszonen satt till $TIMEZONE."
  else
    echo "VARNING: kunde inte sätta tidszonen $TIMEZONE (finns den i tzdata?)"
  fi
fi

# --- Koden -----------------------------------------------------------------
if [[ -d "$APP_DIR/.git" ]]; then
  echo "Uppdaterar koden..."
  git -C "$APP_DIR" fetch --quiet origin main
  git -C "$APP_DIR" reset --hard --quiet origin/main
else
  echo "Hamtar koden..."
  rm -rf "$APP_DIR"
  git clone --quiet --depth 1 "$REPO" "$APP_DIR"
fi

mkdir -p "$DATA_DIR"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

# --- Python-miljo ----------------------------------------------------------
echo "Skapar Python-miljo (OpenCV utan grafik - vi har ingen skarm)..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip

# opencv-python drar in grafikbibliotek som en huvudlos container inte har;
# headless-varianten gor samma sak utan skarm.
grep -v '^opencv-python' "$APP_DIR/requirements.txt" > /tmp/requirements.txt
"$APP_DIR/.venv/bin/pip" install --quiet -r /tmp/requirements.txt
"$APP_DIR/.venv/bin/pip" install --quiet "opencv-python-headless>=4.9"

# --- Installningar ---------------------------------------------------------
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Skriver .env..."
  cat > "$ENV_FILE" <<TEXT
# Skapad av install.sh $(date '+%Y-%m-%d %H:%M')
# Allt gar ocksa att andra i granssnittet pa http://<containerns ip>:${STATUS_PORT}/

# Kameran. ISAPI kraver Basic auth pa den har firmwaren.
CAMERA_IP=${CAMERA_IP}
CAMERA_USER=${CAMERA_USER}
CAMERA_PASSWORD=${CAMERA_PASSWORD}
CAMERA_CHANNEL=101

# Displayen: utsnittet dar siffrorna sitter.
CALIBRATION_ROI=985,0,1620,200
DIGIT_COUNT=4
DECIMALS=2
REQUIRE_ALL_DIGITS=false
REQUIRE_BLANK_FIRST=true
REJECT_ALL_EIGHTS=true
REFERENCE_FILE=
UPSCALE=8
COLOR_CHANNEL=b
THRESHOLD=250

# Rostningen och tiderna.
# RUN_AT anvands bara i lage natt och ar da DATORNS tid for pumpens 02:00
# (pumpens klocka gar efter: matt 2026-09-20 visade den 15:15 nar datorn var
# 15:20). Klockslaget ar bara en startpunkt - korningen tittar pa displayen
# tills den visar 02:00 och tar vardet fran sidan efter den, sedan slutar den.
# Marginalen pa tio minuter tacker att klockan gar olika mycket efter.
# I lage intervall (standard) anvands inte RUN_AT alls.
MIN_CONFIDENCE=0.35
MIN_AGREEMENT=3

# Flodet: sidan efter vardet i varvet. Ligger det kvar hela tiden rinner det -
# da larmar tjansten (granssnittet och Home Assistant), och vardet syns i grafen.
READ_FLOW=true
FLOW_WARN=0.05
FLOW_WARN_MINUTES=30
FLOW_UNIT=l/h
FLOW_EXTRA_S=20
# intervall = laser hela tiden (var EVERY_MINUTES minut, 0 = sa snart den forra
# ar klar) · manuell = bara nar nagon trycker "Las nu" · natt = en gang per dygn.
MODE=${MODE}
EVERY_MINUTES=${EVERY_MINUTES}
RUN_AT=${RUN_AT}
PRE_START_S=600
WINDOW_S=1800
STOP_WHEN_READY=true
INTERVAL_S=1.5
SAVE_FRAMES=false
# Hur lange bilderna fran varje korning sparas. Beviset for den senaste
# lasningen sparas alltid, aven om det skulle vara aldre.
KEEP_DAYS=7
USE_CAMERA_PROFILE=false

# Home Assistant. Lamna tomt om du bara vill ha vardet i granssnittet.
HA_BASE_URL=${HA_URL}
HA_TOKEN=${HA_TOKEN}
HA_LIGHT_ENTITY=
# auto = MQTT om brokern svarar, annars HA:s API. av = bara latest.json
PUBLISH_TO=auto

# MQTT (behovs inte om PUBLISH_TO=av eller rest).
MQTT_HOST=
MQTT_PORT=1883

UNIT=${UNIT}

# Granssnittet.
STATUS_PORT=${STATUS_PORT}
STATUS_BIND=0.0.0.0
STATUS_LIVE=true
STATUS_ALLOW_RUN=true
STATUS_ALLOW_RESTART=true

# Var filerna bor (overlever uppdateringar av koden).
DATA_DIR=${DATA_DIR}
TEXT
else
  echo "Behaller befintlig .env"
fi

# --- Kalibreringen ---------------------------------------------------------
if [[ -n "$CALIBRATION" && -f "$CALIBRATION" ]]; then
  cp "$CALIBRATION" "$DATA_DIR/calibration.json"
  echo "Kalibreringen inlagd."
fi

# --- Tjansten --------------------------------------------------------------
echo "Installerar tjansten..."
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/lxc/vatten-kamera.service" > "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  echo "Tjansten kor."
else
  echo "Tjansten startade inte - sista loggen:"
  journalctl -u "$SERVICE" -n 20 --no-pager || true
fi

IP_ADDR=$(ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)

echo ""
echo "=== Klart ==="
echo "Granssnitt: http://${IP_ADDR:-<den har maskinens ip>}:${STATUS_PORT}/"
echo ""
if [[ ! -f "$DATA_DIR/calibration.json" ]]; then
  echo "KALIBRERINGEN SAKNAS annu - kopiera in den, den ar kamerans och inte"
  echo "datorns, sa den fran din utvecklingsmaskin fungerar. Fran Proxmox-skalet"
  echo "(en ny container har ingen ssh-server, sa scp gar inte dit):"
  echo "    pct push <container-id> calibration.json ${DATA_DIR}/calibration.json"
  echo "eller kor i containern:"
  echo "    cd $APP_DIR && .venv/bin/python main.py calibrate --frames 16 --save"
  echo ""
fi
echo "Loggen:      journalctl -u $SERVICE -f"
echo "Uppdatera:   bash <(curl -fsSL https://raw.githubusercontent.com/nikeng-forenade/vatten_kamera/main/lxc/update.sh)"
