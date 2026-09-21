"""Den nattliga korningen: lampa pa, las displayen, publicera, lampa av.

Vardet visas bara i 10-12 sekunder strax efter 02:00, sa vi kan inte noja oss
med en enstaka bild. Vi startar darfor i god tid innan, tar en bild i sekunden
genom hela fonstret och later en majoritetsrostning avgora vardet. Det ger
ocksa ett skyddsnat: en enstaka suddig bild kan inte forstora nattens lasning.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from camera import CameraError, HikvisionCamera
from config import ROOT, VERSION, Config, load_config
from display_reader import (
    Calibration,
    Consensus,
    Reading,
    ReaderError,
    consensus,
    crop_roi,
    group_similar,
    image_from_crop,
    read_image,
    typical_crops,
)
from ha_client import HaError, HomeAssistant
from mqtt_publisher import MqttPublisher

log = logging.getLogger(__name__)

RUNS_DIR = ROOT / "captures" / "runs"


@dataclass
class RunSummary:
    version: str
    started: str
    finished: str
    frames_taken: int
    frames_readable: int
    value: str | None
    numeric: float | None
    votes: int
    confidence: float
    lamp_used: bool
    frames_dir: str | None = None
    error: str = ""
    # Vad röstningen gjorde, steg för steg — så att en körning går att granska i
    # efterhand utan att kameran behöver köras om.
    page_note: str = ""
    details: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


class NightlyRunner:
    def __init__(self, cfg: Config, *, use_lamp: bool = True) -> None:
        self.cfg = cfg
        self.use_lamp = use_lamp
        self.camera = HikvisionCamera(cfg.camera)
        self.ha = HomeAssistant(cfg.ha)
        self.mqtt = MqttPublisher(cfg.mqtt)
        self.calibration = Calibration.load(cfg.calibration_file)
        # Hur olika tva bilder far vara for att anses visa samma varde.
        self.similarity_threshold = cfg.run.group_threshold
        self.use_camera_profile = cfg.run.use_camera_profile

    # --- Kameralage -------------------------------------------------------

    def _apply_camera_profile(self) -> str:
        """Lanar kameran till lasprofilen och returnerar laget som ska tillbaka.

        Kameran anvands normalt till att se rummet, och i det laget branner den
        sjalvlysande displayen ut till en vit klump. Vi byter darfor bara lage
        under sjalva lasningen och lagger tillbaka det direkt efterat.
        """
        from camera_settings import CameraSettings, CameraSettingsError

        try:
            settings = CameraSettings(self.cfg.camera)
            profile = settings.load_profile()
            if not profile:
                log.info("ingen lasprofil sparad - kameralaget lamnas som det ar")
                return ""

            original = settings.capture()
            settings.apply(profile)
            log.info(
                "kameran lanas till lasprofilen: %s",
                ", ".join(f"{key}={value}" for key, value in profile.items()),
            )
            return original
        except CameraSettingsError as exc:
            log.error("kunde inte byta kameralage: %s", exc)
            return ""

    def _restore_camera(self, original_xml: str) -> None:
        """Lagger tillbaka kamerans eget lage."""
        if not original_xml:
            return

        from camera_settings import CameraSettings, CameraSettingsError

        try:
            CameraSettings(self.cfg.camera).push(original_xml)
            log.info("kameran aterstalld till sitt eget lage")
        except CameraSettingsError as exc:
            log.error("KUNDE INTE ATERSTALLA KAMERAN: %s", exc)

    # --- Tid ---------------------------------------------------------------

    def next_run_time(self, now: datetime | None = None) -> datetime:
        """Nasta gang klockslaget intraffar."""
        now = now or datetime.now()
        try:
            hour, minute, second = (int(part) for part in self.cfg.run.run_at.split(":"))
        except ValueError as exc:
            raise ValueError(f"RUN_AT maste vara HH:MM:SS, inte {self.cfg.run.run_at!r}") from exc

        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def wait_until(self, target: datetime) -> None:
        """Sover till klockslaget, i kortare steg sa att avbrott fungerar."""
        while True:
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                return
            if remaining > 5:
                log.info("vantar %.1f min till %s", remaining / 60.0, target.strftime("%H:%M:%S"))
                time.sleep(min(remaining - 2, 60.0))
            else:
                time.sleep(min(remaining, 0.25))

    # --- Lasning -----------------------------------------------------------

    def _capture_and_read(
        self,
        *,
        duration_s: float,
        frames_dir: Path | None,
    ) -> tuple[list[Reading], list[tuple[Reading, Path]]]:
        """Tar bilder under `duration_s`, vager samman lika bilder och tolkar dem.

        Displayen flimrar: nagra bilder tas mitt i en uppdatering och har svagare
        siffror. Vi samlar darfor alla utsnitt forst, slar ihop de som visar
        samma sida och tolkar den typiska bilden (medianen pixel for pixel).
        Medianen vags inte ner av de svaga bilderna, sa siffrorna star kvar.
        """
        cfg = self.cfg.run
        reader_cfg = self.cfg.reader

        crops: list[np.ndarray] = []
        jpegs: list[bytes | None] = []
        deadline = time.time() + duration_s
        index = 0

        while time.time() < deadline:
            frame_started = time.time()
            index += 1

            try:
                frame = self.camera.snapshot()
            except CameraError as exc:
                log.warning("bild %d misslyckades: %s", index, exc)
                time.sleep(max(0.0, cfg.interval_s - (time.time() - frame_started)))
                continue

            try:
                crop = crop_roi(frame.image, self.calibration.roi, reader_cfg.channel)
            except ReaderError as exc:
                log.warning("kunde inte lasa ut ROI ur bild %d: %s", index, exc)
                time.sleep(max(0.0, cfg.interval_s - (time.time() - frame_started)))
                continue

            crops.append(crop)
            jpegs.append(frame.jpeg if (frames_dir is not None and cfg.save_frames) else None)
            log.debug("bild %d tagen", index)

            time.sleep(max(0.0, cfg.interval_s - (time.time() - frame_started)))

        if not crops:
            return [], []

        groups = group_similar(crops, self.similarity_threshold)
        log.info("%d bilder i %d grupper (samma sida slas ihop till en typisk bild)",
                 len(crops), len(groups))

        readings: list[Reading] = []
        saved: list[tuple[Reading, Path]] = []

        for group_number, indices in enumerate(groups, start=1):
            typical = typical_crops(crops, indices)
            reading = read_image(
                image_from_crop(typical, self.calibration.roi),
                self.calibration,
                reader_cfg,
                timestamp=float(len(indices)),
            )
            # Rosten vager lika tungt som antalet bilder i gruppen. Annars kan
            # tio bilder av samma sida bara bli en enda rost i rostningen.
            reading.weight = len(indices)
            readings.append(reading)

            if reading.ok:
                log.info(
                    "grupp %d (%d bilder): %r konfidens %.2f",
                    group_number, len(indices), reading.value, reading.confidence,
                )
            else:
                log.info("grupp %d (%d bilder): inget varde", group_number, len(indices))

            if frames_dir is not None and cfg.save_frames:
                if not cfg.save_only_success or reading.ok:
                    middle = indices[len(indices) // 2]
                    payload = jpegs[middle]
                    if payload:
                        frames_dir.mkdir(parents=True, exist_ok=True)
                        path = frames_dir / (
                            f"grupp{group_number:02d}_{len(indices)}bilder_"
                            f"{_safe_name(reading.value)}.jpg"
                        )
                        path.write_bytes(payload)
                        saved.append((reading, path))

        return readings, saved

    def _pick_evidence(self, saved: list[tuple[Reading, Path]], value: str | None) -> Path | None:
        """Valjer den bild som bast visar det varde vi kom fram till."""
        if not saved or value is None:
            return None
        matching = [(r, p) for r, p in saved if r.value == value]
        if not matching:
            return None
        best = max(matching, key=lambda item: item[0].confidence)
        return best[1]

    # --- Korning -----------------------------------------------------------

    def run_once(
        self,
        *,
        duration_s: float | None = None,
        save: bool = True,
    ) -> RunSummary:
        """En komplett korning: lampa pa -> las -> publicera -> lampa av."""
        cfg = self.cfg.run
        duration = duration_s if duration_s is not None else cfg.window_s
        started = datetime.now()

        run_dir = RUNS_DIR / started.strftime("%Y%m%d_%H%M%S") if save else None
        lamp_is_on = False
        error = ""
        page_note = ""
        original_camera_xml = ""
        readings: list[Reading] = []
        details: list[dict] = []
        result = Consensus(value=None, votes=0, total=0, confidence=0.0)

        log.info("startar korning (version %s), fonster %.0f s", VERSION, duration)

        try:
            self.mqtt.connect()

            if self.use_camera_profile:
                original_camera_xml = self._apply_camera_profile()

            if self.use_lamp and self.ha.has_lamp:
                try:
                    lamp_is_on = self.ha.lamp_on()
                    if lamp_is_on:
                        log.info(
                            "vantar %.1f s pa att lampan och kameran staller in sig",
                            cfg.lamp_warmup_s,
                        )
                        time.sleep(cfg.lamp_warmup_s)
                except HaError as exc:
                    log.error("kunde inte tanda lampan: %s", exc)
                    error = f"lampan: {exc}"
            elif self.use_lamp:
                log.warning("ingen lampa konfigurerad - laser utan belysning")

            readings, saved = self._capture_and_read(duration_s=duration, frames_dir=run_dir)

            # Vardet som ska ut visas strax efter att pumpen slog om till spolning
            # (02:00). Sidvarvet ar: klockan -> spolttiden -> VARDET -> flodet,
            # och bada vardesidorna (t.ex. 0.91 och 0.00) ser likadana ut for
            # avlasaren: den forsta positionen slackt och tre siffror. Bara
            # ordningen i varvet skiljer dem at, och forst kommer vardet. Darfor
            # rostas bara det som kommer efter spolttidssidan.
            target, page_note = voting_targets(readings)
            voted = {id(reading) for reading in target}
            details = [
                {
                    "grupp": number,
                    "bilder": max(1, reading.weight),
                    "sida": page_kind(reading),
                    "varde": reading.value,
                    "konfidens": round(reading.confidence, 2),
                    "rostad": id(reading) in voted,
                }
                for number, reading in enumerate(readings, start=1)
            ]
            if page_note:
                log.warning("%s", page_note)
            elif len(target) == len(readings):
                log.info("ingen spolttidssida i fonstret - rostar om alla %d lasningar", len(target))
            else:
                log.info(
                    "rostar om %d av %d lasningar - de som foljer pa spolttidssidan",
                    len(target),
                    len(readings),
                )

            result = consensus(
                target,
                min_agreement=cfg.min_agreement,
                min_confidence=cfg.min_confidence,
                decimals=self.cfg.reader.decimals,
            )
            log.info(
                "rostning: %s (%d roster, konfidens %.2f)",
                result.value or "inget varde",
                result.votes,
                result.confidence,
            )

            evidence = self._pick_evidence(saved, result.value)

            if result.ok:
                self.mqtt.publish_result(
                    result,
                    read_at=datetime.now(),
                    image=str(evidence) if evidence else None,
                )
            else:
                reason = _failure_reason(readings, cfg.min_confidence, note=page_note)
                self.mqtt.publish_failure(reason)
                if cfg.notify_on_failure and self.ha.configured:
                    self.ha.notify(f"Kunde inte lasa pumpdisplayen: {reason}")
                error = error or reason

        except Exception as exc:  # noqa: BLE001 - en nattlig korning far aldrig krascha tyst
            log.exception("ovantat fel under korningen")
            error = str(exc)
        finally:
            if lamp_is_on:
                try:
                    self.ha.lamp_off()
                except HaError as exc:
                    log.error("kunde inte slacka lampan: %s", exc)

            self._restore_camera(original_camera_xml)

            summary = RunSummary(
                version=VERSION,
                started=started.isoformat(timespec="seconds"),
                finished=datetime.now().isoformat(timespec="seconds"),
                frames_taken=sum(max(1, r.weight) for r in readings),
                frames_readable=sum(max(1, r.weight) for r in readings if r.ok),
                value=result.value,
                numeric=result.numeric,
                votes=result.votes,
                confidence=result.confidence,
                lamp_used=lamp_is_on,
                frames_dir=str(run_dir) if run_dir else None,
                error=error,
                page_note=page_note,
                details=details,
            )

            if run_dir is not None:
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "summary.json").write_text(summary.to_json(), encoding="utf-8")

            self.mqtt.disconnect()
            self.camera.close()

        return summary

    def run_forever(self) -> None:
        """Vantar in nasta klockslag och kor, om och om igen."""
        while True:
            target = self.next_run_time()
            start_at = target - timedelta(seconds=self.cfg.run.pre_start_s)
            log.info("nasta korning startar %s", start_at.strftime("%Y-%m-%d %H:%M:%S"))

            self.wait_until(start_at)
            summary = self.run_once()

            log.info(
                "klar: varde=%s roster=%d konfidens=%.2f fel=%s",
                summary.value, summary.votes, summary.confidence, summary.error or "-",
            )

            # Sov vidare till strax efter att korningen borde vara slut.
            next_target = self.next_run_time()
            self.wait_until(next_target - timedelta(seconds=self.cfg.run.pre_start_s))


def page_kind(reading: Reading) -> str:
    """Vad displayen visade i den har lasningen.

    Displayen vaxlar mellan tidssidor (klockan och spolttiden, alla fyra
    positioner tands) och vardesidor (vardet star i position 2-4, den forsta ar
    slackt). Tidssidorna far konfidens 0 av `REQUIRE_BLANK_FIRST`, sa en lasning
    med konfidens kvar ar en vardesida.
    """
    if not reading.digits:
        return "okant"
    if all(not digit.blank for digit in reading.digits):
        return "tid"
    if reading.ok and reading.confidence > 0.0:
        return "varde"
    return "okant"


# Hur manga bilder en grupp maste vila pa for att raknas som en SIDA pa displayen
# och inte som en toning mellan tva sidor. Nar vardet halls kvar star displayen
# stilla i 10-12 s, och da blir hela sidan EN grupp pa 14-20 bilder, medan en
# toning bara ger en eller ett par bilder. Uppmatt pa korningarna
# captures/runs/20260920_2101-2108: vardesidan 14-20 bilder, toningarna 1 bild.
PAGE_MIN_FRAMES = 4


def is_recharge_page(reading: Reading) -> bool:
    """Visar den har lasningen spolttidssidan (displayens 02:00)?"""
    return (reading.value or "").strip() == "0200"


def has_recharge_page(readings: list[Reading]) -> bool:
    """Syntes spolttidssidan (displayens 02:00) i den har korningen?"""
    return any(is_recharge_page(reading) for reading in readings)


def voting_targets(readings: list[Reading]) -> tuple[list[Reading], str]:
    """Vilka lasningar som far ligga till grund for rostningen, och varfor inte fler.

    Returnerar lasningarna och en anteckning. En tom lista betyder att inget
    varde far publiceras: antingen syntes spolttidssidan bara sist i fonstret,
    eller sa stod vardesidan dar utan att ga att lasa. Flodessidan ser precis
    likadan ut som vardesidan for avlasaren, och den kommer efterat - den far
    aldrig publiceras i stallet.
    """
    target, unreadable = _voting_targets(readings)
    if target:
        return target, ""
    if unreadable:
        return [], (
            "vardesidan stod dar men gick inte att lasa, och flodessidan som foljer"
            " gar inte att skilja fran den - inget varde publiceras"
        )
    if has_recharge_page(readings):
        return [], (
            "spolttidssidan syntes bara sist i fonstret, sa ingen vardesida kom efter"
            " den - vardet och flodet gar inte att skilja at"
        )
    return list(readings), ""


def readings_after_recharge(readings: list[Reading]) -> list[Reading]:
    """Lasningarna pa vardesidan som foljer pa spolttidssidan (displayens 02:00).

    Vardet som ska ut visas strax efter att pumpen slar om till spolning. Resten
    av dygnet vaxlar displayen mellan klockan, spolttiden, vardet och flodet -
    och bada vardesidorna ser likadana ut for avlasaren, sa det ar bara
    ordningen i varvet som skiljer dem at. Vardet kommer forst.

    Tva saker gjorde att en korning tappade vardet fastan det syntes i bilderna:

    * En toning mellan tva sidor blir en egen liten grupp. Lastes den som ett
      annat varde kapade den svepet direkt, och den stora gruppen med ratt varde
      kom aldrig med i rostningen. (Korningen 21:01: en bild lastes som '0891',
      och de 14 bilderna pa 0.91 forsvann.)
    * Vardesidan kan delas i flera grupper nar displayen flimrar. Da vager bara
      en av dem, och en grupp pa en enda bild nar inte upp i MIN_AGREEMENT.
      (Korningen 21:05: sidan la i grupper om 1+1+1+1+15 bilder, och bara den
      sista fick ligga till grund.)

    Sidan letas darfor upp i tva steg: forst VARDET - fran den forsta grupp som
    vilar pa tillrackligt manga bilder for att vara en sida och inte en toning -
    och sedan ALLA lasningar med det vardet, sa att hela sidan vager.

    Returnerar en tom lista om spolttidssidan inte syntes i korningen - da far
    hela korningen ligga till grund for rostningen i stallet.
    """
    return _voting_targets(readings)[0]


def _voting_targets(readings: list[Reading]) -> tuple[list[Reading], bool]:
    """Rostningsunderlaget, och om vardesidan stod dar utan att kunna lasas."""
    anchors = [index for index, reading in enumerate(readings) if is_recharge_page(reading)]
    if not anchors:
        return [], False

    chosen: list[Reading] = []
    seen: set[int] = set()
    unreadable = False

    for index in anchors:
        page, blocked = _value_page_window(readings, index)
        unreadable = unreadable or blocked
        if not page:
            continue
        value = _page_value(readings, page)
        if value is None:
            continue
        for position in page:
            if readings[position].value == value and position not in seen:
                seen.add(position)
                chosen.append(readings[position])

    return chosen, unreadable


def _value_page_window(readings: list[Reading], anchor: int) -> tuple[list[int], bool]:
    """Index for vardelasningarna efter spolttidssidan, fram till nasta tidssida.

    Tidssidor mellan spolttiden och vardet hoppas over: flera grupper med 02:00 i
    rad ar samma sida delad av flimmer, och da visar displayen anda vardesidan
    harnast. Forst nar vardelasningarna borjat marker en tidssida att svepet ar
    slut.

    En grupp som varken ar en tidssida eller en last vardesida ar antingen en
    toning (en bild) eller en sida som inte gick att lasa (manga bilder). Den
    senare ar vardesidan - korningen 17:14 matte 15 bilder pa vardet 0.64 till
    konfidens 0.00, och rostningen fortsatte till flodessidan och publicerade
    0.00. Da ar det battre att inget varde publiceras, och det sags de av att
    `blocked` returneras.
    """
    window: list[int] = []
    for position in range(anchor + 1, len(readings)):
        kind = page_kind(readings[position])
        if kind == "tid":
            if window:
                break
            continue
        if kind == "varde":
            window.append(position)
            continue
        if not window and max(1, readings[position].weight) >= PAGE_MIN_FRAMES:
            return window, True
    return window, False


def _page_value(readings: list[Reading], window: list[int]) -> str | None:
    """Vardet pa den sida fonstret visar forst.

    Den forsta gruppen i fonstret kan vara en toning som last fel - en
    halvfardig siffra ger ett varde som inte finns pa displayen. En riktig sida
    vilar pa manga bilder, sa vardet tas fran den forsta grupp som ar stor nog
    att vara en sida. Finns ingen sadan (ett kort fonster) tas den forsta
    gruppen, och da ar ordningen i sidvarvet enda ledtraden.
    """
    solid = [
        position for position in window if max(1, readings[position].weight) >= PAGE_MIN_FRAMES
    ]
    first = solid[0] if solid else window[0]
    return readings[first].value


def _safe_name(value: str | None) -> str:
    """Gor om ett last varde till nagot som gar att anvanda i ett filnamn.

    Windows tillater inte tecken som '?' och ':' i filnamn, och ett osakert
    varde kan innehalla precis vad som helst.
    """
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "", value or "")
    return cleaned or "x"


def _failure_reason(readings: list[Reading], min_confidence: float, *, note: str = "") -> str:
    if not readings:
        return "inga bilder kunde tas"
    if note:
        return note
    total = sum(max(1, r.weight) for r in readings)
    usable = [r for r in readings if r.ok]
    if not usable:
        return f"inget varde kunde lasas i {total} bilder"
    return (
        f"for fa eniga lasningar (basta varde {max(r.confidence for r in usable):.2f} "
        f"mot kravet {min_confidence:.2f})"
    )


def main() -> int:
    """Kor en gang direkt - bra for att testa: python pipeline.py --nu"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Nattlig lasning av vattenkameran")
    parser.add_argument("--nu", action="store_true", help="kor direkt i stallet for att vanta")
    parser.add_argument("--sekunder", type=float, help="hur lange vi laser")
    parser.add_argument("--utan-lampa", action="store_true", help="rör inte lampan")
    args = parser.parse_args()

    cfg = load_config()
    runner = NightlyRunner(cfg, use_lamp=not args.utan_lampa)

    if args.nu:
        summary = runner.run_once(duration_s=args.sekunder)
        print(summary.to_json())
    else:
        runner.run_forever()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
