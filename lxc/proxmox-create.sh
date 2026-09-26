#!/usr/bin/env bash
# Vattenkamera - skapa en LXC i Proxmox VE.
#
# Kor pa Proxmox-hostens skal:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/nikeng-forenade/vatten_kamera/main/lxc/proxmox-create.sh)"
#
# Eller med allt ifyllt (byta ut adresserna mot dina egna):
#   bash proxmox-create.sh 210 local-lvm vmbr0 <containerns-ip>/24 <gateway> \
#     --camera-ip <kamerans adress> --camera-password hemligt --unit l
#
# Containern ar huvudlos: ingen skarm, inget skrivbord. Lasningen skots av en
# systemtjanst, och allt styrs och foljs i webblasaren pa http://<ip>:8099/.

set -e

GITHUB_RAW="https://raw.githubusercontent.com/nikeng-forenade/vatten_kamera/main/lxc"
INSTALL_SCRIPT_URL="${GITHUB_RAW}/install.sh"

if [[ -f "$(dirname "$0")/install.sh" ]]; then
  INSTALL_SCRIPT="$(cd "$(dirname "$0")" && pwd)/install.sh"
else
  INSTALL_SCRIPT="/tmp/vattenkamera-install.sh"
  echo "Hamtar installationsskriptet..."
  wget -qO "$INSTALL_SCRIPT" "$INSTALL_SCRIPT_URL" || {
    echo "FEL: kunde inte hamta $INSTALL_SCRIPT_URL"
    exit 1
  }
fi

# --- Standardvarden --------------------------------------------------------
CT_ID="210"
STORAGE="local-lvm"
BRIDGE="vmbr0"
IP="dhcp"
GATEWAY=""
CORES="1"
MEMORY="512"
DISK="4"
INSTALL_OPTS=""
CALIBRATION=""

show_help() {
  echo "Anvandning: bash proxmox-create.sh [CT_ID] [LAGRING] [BRYGGA] [IP/CIDR] [GATEWAY] [flaggor]"
  echo ""
  echo "Flaggor (skickas vidare till install.sh):"
  echo "  --run-at HH:MM:SS        Klockslaget da vardet visas (DATORNS tid - pumpens klocka gar efter)"
  echo "  --camera-ip IP           Kamerans adress"
  echo "  --camera-user NAMN       Kamerans anvandare"
  echo "  --camera-password LOSEN  Kamerans losenord"
  echo "  --ha-url URL             Home Assistant, t.ex. http://homeassistant.local:8123"
  echo "  --ha-token TOKEN         Langlivad token"
  echo "  --unit ENHET             Enhet vid sensorn, t.ex. l"
  echo "  --calibration FIL        Kalibreringsfil pa den har maskinen (kopieras in i containern)"
  echo "  --cores N                Antal karnor (standard 1)"
  echo "  --memory MB              Minne i MB (standard 512)"
  echo "  --disk GB                Disk i GB (standard 4)"
  echo "  --status-port PORT       Port for granssnittet (standard 8099)"
  echo "  --help                   Visa det har"
  exit 0
}

POS_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) show_help ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --run-at|--camera-ip|--camera-user|--camera-password|--ha-url|--ha-token|--unit|--status-port)
      INSTALL_OPTS="$INSTALL_OPTS $1 $2"; shift 2 ;;
    --calibration) CALIBRATION="$2"; shift 2 ;;
    --*) echo "Okand flagga: $1"; exit 1 ;;
    *) POS_ARGS+=("$1"); shift ;;
  esac
done

CT_ID="${POS_ARGS[0]:-$CT_ID}"
STORAGE="${POS_ARGS[1]:-$STORAGE}"
BRIDGE="${POS_ARGS[2]:-$BRIDGE}"
IP="${POS_ARGS[3]:-$IP}"
GATEWAY="${POS_ARGS[4]:-$GATEWAY}"

# --- Interaktivt lage ------------------------------------------------------
if [[ ${#POS_ARGS[@]} -eq 0 ]] && [[ -z "$INSTALL_OPTS" ]] && [[ -z "$CALIBRATION" ]]; then
  echo ""
  echo "  +-------------------------------------------+"
  echo "  |        Vattenkamera - LXC i Proxmox        |"
  echo "  +-------------------------------------------+"
  echo ""
  echo "  Standard: DHCP, 1 karna, 512 MB, granssnitt pa 8099"
  echo "  Anpassat: egen IP, laslaget, kamerans losenord, Home Assistant"
  echo ""
  read -r -p "  Standard [d] eller anpassat [a]? (d/a): " MODE
  echo ""

  if [[ "$MODE" =~ ^[Aa]$ ]]; then
    read -r -p "  Container-ID [210]: " input; CT_ID="${input:-210}"
    read -r -p "  Lagring [local-lvm]: " input; STORAGE="${input:-local-lvm}"
    read -r -p "  Brygga [vmbr0]: " input; BRIDGE="${input:-vmbr0}"
    read -r -p "  IP/CIDR [dhcp]: " input; IP="${input:-dhcp}"
    if [[ "$IP" != "dhcp" ]]; then
      read -r -p "  Gateway: " input; GATEWAY="${input:-}"
    fi
    read -r -p "  Kamerans adress: " input
    INSTALL_OPTS="$INSTALL_OPTS --camera-ip ${input}"
    read -r -p "  Kamerans anvandare [admin]: " input
    INSTALL_OPTS="$INSTALL_OPTS --camera-user ${input:-admin}"
    read -r -p "  Kamerans losenord: " input
    [[ -n "$input" ]] && INSTALL_OPTS="$INSTALL_OPTS --camera-password $input"
    read -r -p "  Laslaget - intervall, manuell eller natt [intervall]: " input
    LASLAGE="${input:-intervall}"
    INSTALL_OPTS="$INSTALL_OPTS --mode $LASLAGE"
    if [[ "$LASLAGE" == "natt" ]]; then
      # Bara natt-laget bryr sig om klockslaget - annars laser den ju hela tiden
      # (och vardet tas anda fran sidan efter att displayen visat 02:00).
      read -r -p "  Klockslag for nattlasningen [02:05:00]: " input
      INSTALL_OPTS="$INSTALL_OPTS --run-at ${input:-02:05:00}"
    fi
    read -r -p "  Kalibreringsfil pa den har maskinen (blank = kalibrera i containern): " input
    [[ -n "$input" ]] && CALIBRATION="$input"
    read -r -p "  Home Assistant-adress (blank = hoppa over): " input
    if [[ -n "$input" ]]; then
      INSTALL_OPTS="$INSTALL_OPTS --ha-url $input"
      read -r -p "  Langlivad token: " input
      INSTALL_OPTS="$INSTALL_OPTS --ha-token $input"
    fi
  fi
fi

sanitize() { echo "$1" | tr -d '\r\n'; }
CT_ID=$(sanitize "$CT_ID")
STORAGE=$(sanitize "$STORAGE")
BRIDGE=$(sanitize "$BRIDGE")
IP=$(sanitize "$IP")
GATEWAY=$(sanitize "$GATEWAY")
CALIBRATION=$(sanitize "$CALIBRATION")

if [[ "$IP" != "dhcp" && "$IP" != */* ]]; then
  IP="${IP}/24"
  echo "Lade till /24 sjalv -> $IP"
fi

if pct status "$CT_ID" &>/dev/null; then
  echo "FEL: container $CT_ID finns redan. Valj ett annat ID eller ta bort den forst."
  exit 1
fi

# --- Mall ------------------------------------------------------------------
if ! pveam list local 2>/dev/null | grep -q debian-12; then
  echo "Uppdaterar mallistan och letar efter Debian 12..."
  pveam update >/dev/null 2>&1 || true
fi

TEMPLATE=$(pveam available --section system 2>/dev/null | grep -o 'debian-12-standard[^ ]*' | sort | tail -1)
if [[ -z "$TEMPLATE" ]]; then
  TEMPLATE="debian-12-standard_12.7-1_amd64.tar.zst"
fi

if ! pveam list local 2>/dev/null | grep -q "$TEMPLATE"; then
  echo "Hamtar mallen $TEMPLATE..."
  pveam download local "$TEMPLATE" || {
    echo "FEL: kunde inte hamta mallen. Kor 'pveam available --section system' och valj en debian-12 sjalv."
    exit 1
  }
fi

echo ""
echo "=== Vattenkamera - ny LXC ==="
echo "ID:        $CT_ID"
echo "Lagring:   $STORAGE ($DISK GB, $CORES karna, $MEMORY MB)"
echo "Natverk:   $BRIDGE $IP ${GATEWAY:+gw=$GATEWAY}"
echo "Installer: ${INSTALL_OPTS:-standard}"
echo "Kalibrering: ${CALIBRATION:-ingen (kalibreras i containern)}"
echo ""

NET0="name=eth0,bridge=${BRIDGE},ip=${IP}"
if [[ "$IP" != "dhcp" && -n "$GATEWAY" ]]; then
  NET0="${NET0},gw=${GATEWAY}"
fi

pct create "$CT_ID" "local:vztmpl/${TEMPLATE}" \
  --hostname "vattenkamera" \
  --rootfs "${STORAGE}:${DISK}" \
  --memory "$MEMORY" \
  --cores "$CORES" \
  --net0 "$NET0" \
  --unprivileged 1 \
  --features "nesting=1" \
  --onboot 1 \
  --start 1

echo "Containern ar skapad. Vantar pa att den startar..."
for _ in $(seq 1 30); do
  if pct exec "$CT_ID" -- true 2>/dev/null; then break; fi
  sleep 2
done

echo "Installerar grundpaket..."
pct exec "$CT_ID" -- env LC_ALL=C bash -c "apt-get update -qq && apt-get install -y -qq curl git ca-certificates >/dev/null"

echo "Skickar in installationsskriptet..."
pct push "$CT_ID" "$INSTALL_SCRIPT" /root/install.sh

if [[ -n "$CALIBRATION" ]]; then
  if [[ ! -f "$CALIBRATION" ]]; then
    echo "FEL: hittar inte kalibreringsfilen $CALIBRATION"
    exit 1
  fi
  echo "Skickar in kalibreringen..."
  pct push "$CT_ID" "$CALIBRATION" /root/calibration.json
  INSTALL_OPTS="$INSTALL_OPTS --calibration /root/calibration.json"
fi

echo "Kor installationen (det tar nagra minuter - Python och OpenCV ska byggas)..."
if [[ -n "$INSTALL_OPTS" ]]; then
  pct exec "$CT_ID" -- env LC_ALL=C bash /root/install.sh $INSTALL_OPTS
else
  pct exec "$CT_ID" -- env LC_ALL=C bash /root/install.sh
fi

IP_ADDR=$(pct exec "$CT_ID" -- ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
PORT=$(echo "$INSTALL_OPTS" | grep -oP '(?<=--status-port )\d+' || echo "8099")

echo ""
echo "=============================================================="
echo "  Vattenkamera kor i container $CT_ID"
echo ""
echo "  Granssnitt:  http://${IP_ADDR:-<containerns ip>}:${PORT}/"
echo ""
echo "  Kvar att gora:"
if [[ -n "$CALIBRATION" ]]; then
  echo "    1. Kalibreringen ar inlagd."
else
  echo "    1. Lagg in kalibreringen (kalibreringen ar kamerans, inte datorns):"
  echo "         scp calibration.json root@${IP_ADDR:-<ip>}:/opt/vattenkamera/data/"
  echo "       eller kor 'main.py calibrate --frames 16 --save' i containern."
fi
echo "    2. Oppna granssnittet och klicka 'Testa kameran' och 'Las nu'."
echo "=============================================================="
