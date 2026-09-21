"""Installningarna som gar att andra i webbgranssnittet.

Allt ligger i `.env` - samma fil som programmet redan laser - sa att
granssnittet och kommandoraden aldrig kan hamna i otakt. Bara raden for den
nyckel som andras skrivs om, sa att kommentarerna i filen star kvar och filen
fortfarande gar att lasa sjalv.

Ett hemligt varde (losenord, token) lamnas aldrig ut: granssnittet far veta ATT
det finns ett varde, inte vad det ar. Skickas ett tomt hemligt falt tillbaka
behalls det gamla vardet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import ROOT

ENV_FILE = ROOT / ".env"

# Rubriken som laggs framfor nycklar som tillkommit via granssnittet.
NEW_SECTION = "# Andrat i webbgranssnittet"


@dataclass(frozen=True)
class Field:
    """En installning som gar att andra."""

    key: str
    label: str
    group: str
    kind: str = "text"  # text, int, float, bool, choice, secret, time
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    keep_if_empty: bool = False
    # Vardet som galler nar nyckeln inte star i .env. Utan det skulle
    # granssnittet visa "av" for en installning som i sjalva verket ar pa - och
    # skriva ner det nar man trycker Spara.
    default: str = ""
    # Vissa installningar hor bara till ett lage (t.ex. klockslaget i lage natt).
    # Da visas de bara nar det laget ar valt - annars ligger de kvar i .env utan
    # att skrapa i vagen.
    only_in_mode: str = ""

    def as_dict(self, value: str) -> dict:
        """Faltet som granssnittet ritar upp det."""
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "help": self.help,
            "choices": list(self.choices),
            "min": self.minimum,
            "max": self.maximum,
            "value": "" if self.kind == "secret" else value,
            "har_varde": bool(value),
            "bara_i_lage": self.only_in_mode,
        }


def _field(key: str, label: str, group: str, **kwargs) -> Field:
    return Field(key=key, label=label, group=group, **kwargs)


# Ordningen har styr ordningen i granssnittet.
FIELDS: tuple[Field, ...] = (
    # --- Tiderna ---------------------------------------------------------
    _field(
        "MODE",
        "Laslaget",
        "Tider",
        kind="choice",
        choices=("intervall", "manuell", "natt"),
        default="intervall",
        help=(
            "intervall = laser hela tiden, en lasning var X minut (standard) · "
            "manuell = laser bara nar du trycker Las nu · "
            "natt = en gang per dygn vid klockslaget"
        ),
    ),
    _field(
        "EVERY_MINUTES",
        "Las var X minut",
        "Tider",
        kind="float",
        minimum=0,
        maximum=1440,
        default="5",
        only_in_mode="intervall",
        help=(
            "0 = hela tiden (nasta lasning startar strax efter den forra) · "
            "5 = var femte minut. En lasning tar ca en minut."
        ),
    ),
    _field(
        "RUN_AT",
        "Klockslag for lasningen (datorns tid)",
        "Tider",
        kind="time",
        only_in_mode="natt",
        help=(
            "Bara i lage natt. Klockslaget ar datorns tid - pumpens klocka gar "
            "efter (matt ~6 min), sa pumpens 02:00 ar datorns ~02:06. Lasningen "
            "tittar anda pa displayen tills den visar 02:00."
        ),
    ),
    _field(
        "PRE_START_S",
        "Starta sa har lange innan (s)",
        "Tider",
        kind="float",
        minimum=0,
        maximum=600,
        only_in_mode="natt",
    ),
    _field(
        "WINDOW_S",
        "Ge inte upp efter (s)",
        "Tider",
        kind="float",
        minimum=10,
        maximum=1800,
        only_in_mode="natt",
    ),
    _field(
        "STOP_WHEN_READY",
        "Sluta sa snart vardet ar fangat",
        "Tider",
        kind="bool",
        default="true",
        help="Av = las hela fonstret ut, aven efter att vardesidan synts",
    ),
    _field("INTERVAL_S", "Tid mellan bilderna (s)", "Tider", kind="float", minimum=0.2, maximum=60),
    # --- Displayen -------------------------------------------------------
    _field(
        "CALIBRATION_ROI",
        "Utsnitt dar displayen sitter",
        "Displayen",
        help="x1,y1,x2,y2 i bilden. Andra bara om kameran flyttats - och kalibrera sedan om.",
    ),
    _field("DIGIT_COUNT", "Antal sifferpositioner", "Displayen", kind="int", minimum=1, maximum=8),
    _field("DECIMALS", "Antal decimaler", "Displayen", kind="int", minimum=0, maximum=3),
    _field("COLOR_CHANNEL", "Fargkanal", "Displayen", kind="choice", choices=("auto", "gray", "r", "g", "b")),
    _field("THRESHOLD", "Troskel (0-255)", "Displayen", kind="int", minimum=0, maximum=255),
    _field("UPSCALE", "Forstoring innan tolkning", "Displayen", kind="float", minimum=1, maximum=16),
    _field("CLIP_BOTTOM", "Klipp bort nedtill (0-1)", "Displayen", kind="float", minimum=0, maximum=1),
    _field("REQUIRE_BLANK_FIRST", "Krav att forsta positionen ar slackt", "Displayen", kind="bool", default="false"),
    _field("REJECT_ALL_EIGHTS", "Forkasta 888", "Displayen", kind="bool", default="false"),
    # --- Rostningen ------------------------------------------------------
    _field("MIN_AGREEMENT", "Minsta antal roster", "Rostningen", kind="int", minimum=1, maximum=100),
    _field("MIN_CONFIDENCE", "Minsta konfidens per siffra", "Rostningen", kind="float", minimum=0, maximum=1),
    _field(
        "STOP_WHEN_READY",
        "Sluta sa snart vardet ar fangat",
        "Rostningen",
        kind="bool",
        default="true",
        help="Av = las hela fonstret ut, aven efter att vardesidan synts",
        only_in_mode="natt",
    ),
    _field(
        "KEEP_DAYS",
        "Spara bilderna i sa har manga dygn",
        "Rostningen",
        kind="int",
        minimum=1,
        maximum=365,
        help="Bevisbilden for den senaste lasningen sparas alltid, aven om den ar aldre",
    ),
    _field("SAVE_FRAMES", "Spara alla bilder fran varje korning", "Rostningen", kind="bool"),
    _field("SAVE_ONLY_SUCCESS", "Spara bara lyckade lasningar", "Rostningen", kind="bool"),
    _field("NOTIFY_ON_FAILURE", "Notis till HA vid misslyckad lasning", "Rostningen", kind="bool"),
    # --- Kameran ---------------------------------------------------------
    # Kamerans adress, anvandare och losenord finns BARA i .env (gitignorerad)
    # och skrivs harifran - aldrig i koden.
    _field(
        "CAMERA_IP",
        "Kamerans adress",
        "Kameran",
        help="Sparas bara i .env pa den har maskinen - den ligger aldrig i koden",
    ),
    _field("CAMERA_USER", "Anvandare", "Kameran"),
    _field("CAMERA_PASSWORD", "Losenord", "Kameran", kind="secret", keep_if_empty=True),
    _field("CAMERA_HTTP_PORT", "Port (ISAPI)", "Kameran", kind="int", minimum=1, maximum=65535, default="80"),
    _field("CAMERA_CHANNEL", "Strom", "Kameran", default="101", help="101 = huvudstrom, 102 = subström"),
    _field("USE_CAMERA_PROFILE", "Lana kameran till lasprofilen", "Kameran", kind="bool"),
    # --- Home Assistant --------------------------------------------------
    _field("HA_BASE_URL", "Adress", "Home Assistant", help="T.ex. http://homeassistant.local:8123"),
    _field("HA_TOKEN", "Langlivad token", "Home Assistant", kind="secret", keep_if_empty=True),
    _field("HA_LIGHT_ENTITY", "Lampan vid pumpen", "Home Assistant", help="Tom = ingen lampstyrning"),
    _field(
        "PUBLISH_TO",
        "Var vardet publiceras",
        "Home Assistant",
        kind="choice",
        choices=("auto", "mqtt", "rest", "bada", "av"),
        help="av = bara senaste.json (HACS-integrationen laser den)",
    ),
    # --- MQTT ------------------------------------------------------------
    _field("MQTT_HOST", "Broker", "MQTT"),
    _field("MQTT_PORT", "Port", "MQTT", kind="int", minimum=1, maximum=65535),
    _field("MQTT_USER", "Anvandare", "MQTT"),
    _field("MQTT_PASSWORD", "Losenord", "MQTT", kind="secret", keep_if_empty=True),
    _field("MQTT_BASE_TOPIC", "Bas-topic", "MQTT"),
    # --- Tjansten --------------------------------------------------------
    _field("STATUS_PORT", "Port for detta granssnitt", "Tjansten", kind="int", minimum=0, maximum=65535, default="8099"),
    _field("STATUS_BIND", "Lyssna pa adress", "Tjansten", default="0.0.0.0"),
    _field(
        "STATUS_LIVE",
        "Live-uppdatering i granssnittet",
        "Tjansten",
        kind="bool",
        default="true",
        help="Av = sidan uppdaterar sig bara nar du ber om det",
    ),
    _field(
        "STATUS_ALLOW_RUN",
        "Tillat lasning direkt fran granssnittet",
        "Tjansten",
        kind="bool",
        default="true",
    ),
    _field(
        "STATUS_ALLOW_RESTART",
        "Tillat omstart av tjansten fran granssnittet",
        "Tjansten",
        kind="bool",
        default="true",
    ),
    _field("UNIT", "Enhet vid sensorn i HA", "Tjansten", help="T.ex. l eller m3. Tom = ingen enhet."),
)

BY_KEY: dict[str, Field] = {item.key: item for item in FIELDS}

# De installningar som INTE kan sla igenom utan omstart: de satts nar
# granssnittets HTTP-server startas (port och adress) eller lastes en gang nar
# den byggdes. Allt annat laser tjansten om mellan korningarna.
RESTART_KEYS = frozenset(
    {
        "STATUS_PORT",
        "STATUS_BIND",
        "STATUS_LIVE",
        "STATUS_ALLOW_RUN",
        "STATUS_ALLOW_RESTART",
    }
)


def restart_required(changed: dict[str, str]) -> list[str]:
    """Vilka av de andrade nycklarna som kraver att tjansten startas om."""
    return sorted(key for key in changed if key in RESTART_KEYS)

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)(:([0-5]?\d))?$")


def _fil(path: Path | None) -> Path:
    """Vilken .env som avses.

    Las vid anropet och inte nar modulen laddas, sa att ett test - eller en
    DATA_DIR nagon annanstans - kan peka ut en annan fil.
    """
    return path if path is not None else ENV_FILE


def read_env(path: Path | None = None) -> dict[str, str]:
    """Laser .env som en vanlig nyckel/varde-tabell."""
    path = _fil(path)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def current(path: Path | None = None) -> list[dict]:
    """Alla falt med vardena som galler just nu (hemliga utan varde).

    Saknas nyckeln i .env visas standardvardet, sa att granssnittet aldrig
    visar "av" for nagot som ar pa.
    """
    values = read_env(path)
    return [item.as_dict(values.get(item.key, item.default)) for item in FIELDS]


def normalize(item: Field, raw: str, *, existing: str = "") -> str | None:
    """Gor om ett inskickat varde till det som ska sta i .env.

    Returnerar None om vardet ska lamnas som det ar (tomt hemligt falt), och
    kastar ValueError med en forklaring om det inte gar att anvanda.
    """
    text = (raw or "").strip()

    if item.kind == "secret":
        return text or None

    if item.kind == "bool":
        if text.lower() in {"true", "1", "ja", "on", "yes"}:
            return "true"
        if text.lower() in {"false", "0", "nej", "off", "no", ""}:
            return "false"
        raise ValueError(f"{item.label}: ska vara true eller false")

    if text == "" and not item.keep_if_empty:
        return ""

    if item.kind in {"int", "float"}:
        try:
            number = int(float(text)) if item.kind == "int" else float(text.replace(",", "."))
        except ValueError as exc:
            raise ValueError(
                f"{item.label}: ska vara ett {'heltal' if item.kind == 'int' else 'tal'}"
            ) from exc
        if item.minimum is not None and number < item.minimum:
            raise ValueError(f"{item.label}: far inte vara mindre an {item.minimum:g}")
        if item.maximum is not None and number > item.maximum:
            raise ValueError(f"{item.label}: far inte vara storre an {item.maximum:g}")
        return str(number) if item.kind == "int" else f"{number:g}"

    if item.kind == "time":
        if not _TIME.match(text):
            raise ValueError(f"{item.label}: ska vara HH:MM eller HH:MM:SS")
        parts = text.split(":")
        while len(parts) < 3:
            parts.append("00")
        return ":".join(f"{int(part):02d}" for part in parts)

    if item.kind == "choice" and text not in item.choices:
        raise ValueError(f"{item.label}: valj ett av {', '.join(item.choices)}")

    if "\n" in text or "\r" in text:
        raise ValueError(f"{item.label}: far inte innehalla radbrytning")

    return text


def apply_changes(updates: dict[str, str], path: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Skriver andringarna i .env. Returnerar (andrade, fel)."""
    path = _fil(path)
    values = read_env(path)
    changed: dict[str, str] = {}
    problems: dict[str, str] = {}

    for key, raw in updates.items():
        item = BY_KEY.get(key)
        if item is None:
            problems[key] = "okand installning"
            continue
        try:
            value = normalize(item, raw, existing=values.get(key, ""))
        except ValueError as exc:
            problems[key] = str(exc)
            continue
        if value is None:
            continue  # tomt hemligt falt: behall det gamla
        if values.get(key) == value:
            continue
        changed[key] = value
        values[key] = value

    if changed:
        write_env(values, changed, path)

    return changed, problems


def write_env(values: dict[str, str], changed: dict[str, str], path: Path | None = None) -> None:
    """Skriver tillbaka .env: bara andrade rader byts, resten star kvar."""
    path = _fil(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in changed:
                out.append(f"{key}={changed[key]}")
                seen.add(key)
                continue
            if key in values:
                seen.add(key)
        out.append(line)

    missing = [key for key in changed if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append(NEW_SECTION)
        for key in missing:
            out.append(f"{key}={changed[key]}")

    text = "\n".join(out).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
