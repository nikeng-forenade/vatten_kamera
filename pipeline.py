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
    average_crops,
    consensus,
    crop_roi,
    group_similar,
    image_from_crop,
    read_image,
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

        Siffrorna ar sma, sa en enstaka bild ar kanslig for sensorns brus. Vi
        samlar darfor alla utsnitt forst, laggar ihop de som visar samma varde
        och tolkar medelvardesbilden. Bruset vags ut medan siffrorna star kvar.
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
        log.info("%d bilder i %d grupper (varden som visar samma sak vags samman)",
                 len(crops), len(groups))

        readings: list[Reading] = []
        saved: list[tuple[Reading, Path]] = []

        for group_number, indices in enumerate(groups, start=1):
            averaged = average_crops(crops, indices)
            reading = read_image(
                image_from_crop(averaged, self.calibration.roi),
                self.calibration,
                reader_cfg,
                timestamp=float(len(indices)),
            )
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
        readings: list[Reading] = []
        result = Consensus(value=None, votes=0, total=0, confidence=0.0)

        log.info("startar korning (version %s), fonster %.0f s", VERSION, duration)

        try:
            self.mqtt.connect()

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

            result = consensus(
                readings,
                min_agreement=cfg.min_agreement,
                min_confidence=cfg.min_confidence,
            )

            evidence = self._pick_evidence(saved, result.value)

            if result.ok:
                self.mqtt.publish_result(
                    result,
                    read_at=datetime.now(),
                    image=str(evidence) if evidence else None,
                )
            else:
                reason = _failure_reason(readings, cfg.min_confidence)
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

            summary = RunSummary(
                version=VERSION,
                started=started.isoformat(timespec="seconds"),
                finished=datetime.now().isoformat(timespec="seconds"),
                frames_taken=len(readings),
                frames_readable=sum(1 for r in readings if r.ok),
                value=result.value,
                numeric=result.numeric,
                votes=result.votes,
                confidence=result.confidence,
                lamp_used=lamp_is_on,
                frames_dir=str(run_dir) if run_dir else None,
                error=error,
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


def _safe_name(value: str | None) -> str:
    """Gor om ett last varde till nagot som gar att anvanda i ett filnamn.

    Windows tillater inte tecken som '?' och ':' i filnamn, och ett osakert
    varde kan innehalla precis vad som helst.
    """
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "", value or "")
    return cleaned or "x"


def _failure_reason(readings: list[Reading], min_confidence: float) -> str:
    if not readings:
        return "inga bilder kunde tas"
    usable = [r for r in readings if r.ok]
    if not usable:
        return f"inget varde kunde lasas i {len(readings)} bilder"
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
