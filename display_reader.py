"""Laser displayens varde ur en kamerabild.

Arbetsgang per bild:
    1. Beskar ROI (displayfonstret) ur helbilden.
    2. Normaliserar ljusstyrkan sa svagt ljus inte spelar roll.
    3. Trosklar fram de tta siffrorna och hittar deras gemensamma band.
    4. Delar bandet i sifferceller och tolkar varje cell som en sjusegmentsiffra.
    5. Ger ett varde + konfidens for hela lasningen.

Eftersom displayen vaxlar mellan olika varden laser vi manga bilder och later
en majoritetsrostning avgora vilket varde som visas.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from config import ReaderConfig
from segments import DecodeResult, decode_cell

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]


class ReaderError(RuntimeError):
    """Kunde inte tolka bilden alls."""


@dataclass
class Calibration:
    """Sitespecifik kalibrering for en fast kamerauppstallning."""

    roi: Box = (0, 0, 0, 0)
    digit_count: int = 4
    # Om satt anvands dessa celler (absoluta pixlar i helbilden) i stallet for
    # att hitta bandet automatiskt. Bra nar displayen har decimalpunkt/kolon.
    cell_boxes: list[Box] = field(default_factory=list)
    # Klammer over cellernas hojd: hur mycket av cellen som ar sjalva siffran.
    notes: str = ""

    @property
    def valid(self) -> bool:
        x1, y1, x2, y2 = self.roi
        return x2 > x1 and y2 > y1

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Calibration:
        if not path.exists():
            raise ReaderError(
                f"kalibreringen saknas ({path}). Kor 'python main.py calibrate' forst."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["roi"] = tuple(data.get("roi", (0, 0, 0, 0)))
        data["cell_boxes"] = [tuple(b) for b in data.get("cell_boxes", [])]
        return cls(**data)


@dataclass
class Reading:
    """En tolkning av en enskild bild."""

    value: str
    confidence: float
    digits: list[DecodeResult]
    timestamp: float
    boxes: list[Box]
    ok: bool = True
    error: str = ""

    @property
    def numeric(self) -> float | None:
        """Vardet som tal, utan separatorer. None om inget varde las gick."""
        digits_only = "".join(ch for ch in self.value if ch.isdigit())
        if not digits_only:
            return None
        return float(digits_only)


@dataclass
class Consensus:
    """Resultatet av en majoritetsrostning over flera bilder."""

    value: str | None
    votes: int
    total: int
    confidence: float

    @property
    def numeric(self) -> float | None:
        if not self.value:
            return None
        digits_only = "".join(ch for ch in self.value if ch.isdigit())
        return float(digits_only) if digits_only else None

    @property
    def ok(self) -> bool:
        return self.value is not None


# ---------------------------------------------------------------------------
# Bildbehandling
# ---------------------------------------------------------------------------


def preprocess(image: np.ndarray, roi: Box, cfg: ReaderConfig) -> tuple[np.ndarray, np.ndarray]:
    """Returnerar (normaliserad float-bild 0..1, trosklad binarbild) for ROI:t."""
    x1, y1, x2, y2 = roi
    h, w = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        raise ReaderError(f"ROI {roi} ligger utanfor bilden {w}x{h}")

    crop = image[y1:y2, x1:x2]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop

    if cfg.clip_bottom > 0:
        cut = int(round(gray.shape[0] * cfg.clip_bottom))
        if 0 < cut < gray.shape[0]:
            gray = gray[: gray.shape[0] - cut, :]

    if cfg.upscale and cfg.upscale != 1.0:
        gray = cv2.resize(
            gray, None, fx=cfg.upscale, fy=cfg.upscale, interpolation=cv2.INTER_CUBIC
        )

    gray = gray.astype(np.float32)

    if cfg.invert:
        gray = 255.0 - gray

    if cfg.normalize:
        # Skala sa att displayens ljusaste punkt blir 1.0.
        hi = float(np.percentile(gray, 99.5))
        if hi > 1.0:
            gray = gray * (255.0 / hi)
        gray = np.clip(gray, 0.0, 255.0)

    # Mild utjamning - tar bort sensorkorn utan att sudda ihop segmenten.
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    normalized = np.clip(gray, 0.0, 255.0) / 255.0

    # Otsu kraver en 8-bitarsbild, sa trosklingen gors pa en uint8-kopia.
    gray_u8 = np.clip(gray, 0.0, 255.0).astype(np.uint8)

    if cfg.threshold:
        _, binary = cv2.threshold(gray_u8, float(cfg.threshold), 255, cv2.THRESH_BINARY)
    else:
        _, binary = cv2.threshold(gray_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    if cfg.close_kernel >= 3:
        kernel = np.ones((cfg.close_kernel, cfg.close_kernel), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    return normalized, binary


def find_band(binary: np.ndarray) -> Box:
    """Hittar det band dar siffrorna sitter.

    Pumphuset och bakgrunden ar stora sammanhangande ljusa ytor medan siffrorna
    ar sma, sa vi filtrerar pa storlek i stallet for pa var de sitter. Det
    sistnamnda vore frestelse men fel: en siffra kan mycket val tangera ROI:ts
    kant om utsnittet ar tight.

    Sifferbandet ar alltid full hogd aven om en siffra ar smal (som en etta),
    sa hojden blir palitlig aven nar rutnatet sedan ska passas in.
    """
    h, w = binary.shape[:2]
    if h == 0 or w == 0:
        raise ReaderError("tomt ROI")

    roi_area = float(h * w)
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)

    # Steg 1: sjalva siffersegmenten. Pumphuset och bakgrunden ar stora
    # sammanhangande ytor och faller bort har.
    segments: list[tuple[float, float, float, float]] = []
    for label in range(1, count):
        x, y, cw, ch, area = stats[label]
        if area < 6:
            continue
        if area > 0.30 * roi_area:
            continue
        if cw > 0.85 * w and ch > 0.85 * h:
            continue
        segments.append((float(x), float(y), float(cw), float(ch)))

    if not segments:
        return 0, 0, w, h

    # Steg 2: breddspannet. Vi utgar fran de komponenter som ligger i samma
    # horisontella band som huvuddelen, sa att en ensam ljusflack (reflex,
    # lampa) inte drar ivag spannet.
    heights = np.array([c[3] for c in segments])
    centers = np.array([c[1] + c[3] / 2.0 for c in segments])
    median_center = float(np.median(centers))
    tolerance = max(float(np.median(heights)) * 1.5, 0.35 * h)

    row_band = [c for c in segments if abs((c[1] + c[3] / 2.0) - median_center) <= tolerance]
    width_source = row_band or segments

    bx1 = int(min(c[0] for c in width_source))
    bx2 = int(max(c[0] + c[2] for c in width_source))

    # Steg 3: hojden. Har tar vi med ALLA siffersegment inom breddspannet, aven
    # de vagrata strecken upptill och nedtill. Utan dem saknar avlasaren halva
    # beviset och en nolla blir latt en etta. Vi haller oss anda till de
    # komponenter som klarade storleksfiltret, sa att en ljus pumphuskant inte
    # drar ut hojden over hela bilden.
    in_columns = [c for c in segments if c[0] + c[2] > bx1 and c[0] < bx2]
    height_source = in_columns or width_source

    by1 = int(min(c[1] for c in height_source))
    by2 = int(max(c[1] + c[3] for c in height_source))

    return bx1, by1, bx2, by2


def _boundary_penalty(binary: np.ndarray, boxes: list[Box]) -> float:
    """Straffar rutnat vars cellgranser skar genom tta segment.

    Ett korrekt rutnat lagger sina granser i de morka mellanrummen mellan
    siffrorna. Skar en grans genom ett tnt segment betyder det att en siffra
    delas mitt itu - och en halv nolla ser ut som en etta, vilket annars kan
    ge en hog konfidens pa helt fel varde.
    """
    if len(boxes) < 2:
        return 0.0

    y1 = boxes[0][1]
    y2 = boxes[0][3]
    if y2 <= y1:
        return 0.0

    penalties: list[float] = []
    for index in range(1, len(boxes)):
        xb = boxes[index][0]
        left = max(0, xb - 1)
        column = binary[y1:y2, left : xb + 2]
        if column.size == 0:
            continue
        penalties.append(float(np.count_nonzero(column)) / float(column.size))

    return float(np.mean(penalties)) if penalties else 0.0


def fit_grid(
    normalized: np.ndarray,
    binary: np.ndarray,
    band: Box,
    digit_count: int,
) -> tuple[list[Box], float]:
    """Hittar den cellindelning som tolkar displayen bast.

    Tre saker gors samtidigt, for de ar sammanflattade i en riktig display:
      * sifferbredd - en siffra fyller inte hela sin plats, det finns ett
        mellanrum mellan siffrorna,
      * delning (pitch) - avstanden mellan siffrorna,
      * forskjutning - var rutnatet borjar.

    En etta lyser bara i hoger halva av sin cell, sa bandet kan dessutom vara
    forskjutet. Vi provar darfor många kombinationer och valjer den som tolkar
    siffrorna med hogst konfidens, utan att skara genom dem och utan att lamna
    tta pixlar utanfor rutnatet.
    """
    img_h, img_w = normalized.shape[:2]
    bx1, by1, bx2, by2 = band
    span = max(1, bx2 - bx1)
    base_pitch = span / float(digit_count)

    band_rows = binary[by1:by2, :]
    lit_per_column = np.count_nonzero(band_rows, axis=0).astype(np.float64)
    total_lit = float(lit_per_column.sum())

    def cells_for(pitch: float, width: float, left0: float) -> list[Box] | None:
        boxes: list[Box] = []
        for index in range(digit_count):
            cx1 = int(round(left0 + index * pitch))
            cx2 = int(round(cx1 + width))
            cx1 = max(0, min(img_w - 1, cx1))
            cx2 = max(0, min(img_w, cx2))
            if cx2 - cx1 < 4:
                return None
            boxes.append((cx1, by1, cx2, by2))
        return boxes

    def unexplained_lit(boxes: list[Box]) -> float:
        """Andel tta pixlar som hamnar utanfor alla celler."""
        if total_lit <= 0:
            return 0.0
        covered = np.zeros(img_w, dtype=bool)
        for cx1, _, cx2, _ in boxes:
            covered[cx1:cx2] = True
        outside = float(lit_per_column[~covered].sum())
        return outside / total_lit

    def score_of(boxes: list[Box]) -> float:
        scores: list[float] = []
        for cx1, cy1, cx2, cy2 in boxes:
            cell = normalized[cy1:cy2, cx1:cx2]
            if cell.size == 0:
                return -1.0
            result = decode_cell(cell)
            # En tom cell ar tillaten (inledande nolla kan vara slackt) men
            # ska vaga latt, sa ett rutnat med riktiga siffror foredras.
            scores.append(0.15 if result.blank else result.confidence)

        return (
            float(np.mean(scores))
            - 0.6 * _boundary_penalty(binary, boxes)
            - 0.6 * unexplained_lit(boxes)
        )

    best_score = -3.0
    best_boxes: list[Box] = []

    for pitch_scale in np.linspace(0.70, 1.45, 31):
        pitch = base_pitch * float(pitch_scale)
        for width_ratio in (0.55, 0.70, 0.85, 1.00):
            width = pitch * width_ratio
            for offset in np.linspace(-0.95, 0.35, 19):
                boxes = cells_for(pitch, width, bx1 + float(offset) * pitch)
                if boxes is None:
                    continue
                score = score_of(boxes)
                if score > best_score:
                    best_score = score
                    best_boxes = boxes

    if not best_boxes:
        # Sista utvagen: jamn indelning av bandet.
        step = span / float(digit_count)
        best_boxes = [
            (
                int(round(bx1 + i * step)),
                by1,
                int(round(bx1 + (i + 1) * step)),
                by2,
            )
            for i in range(digit_count)
        ]
        best_score = 0.0

    return best_boxes, best_score


def read_image(
    image: np.ndarray,
    cal: Calibration,
    cfg: ReaderConfig,
    *,
    timestamp: float = 0.0,
) -> Reading:
    """Laser displayen i en bild och returnerar varde + konfidens."""
    normalized, binary = preprocess(image, cal.roi, cfg)

    if cal.cell_boxes:
        scale = cfg.upscale if cfg.upscale else 1.0
        x1, y1 = cal.roi[0], cal.roi[1]
        boxes = [
            (
                int(round((bx1 - x1) * scale)),
                int(round((by1 - y1) * scale)),
                int(round((bx2 - x1) * scale)),
                int(round((by2 - y1) * scale)),
            )
            for bx1, by1, bx2, by2 in cal.cell_boxes
        ]
    else:
        band = find_band(binary)
        boxes, _grid_score = fit_grid(normalized, binary, band, cal.digit_count)

        # Rutnatet sitter fast sa lange kameran star still, sa vi kommer i hag
        # resultatet. Annars skulle den dyra sokningen goras for varje bild.
        scale = cfg.upscale if cfg.upscale else 1.0
        x1, y1 = cal.roi[0], cal.roi[1]
        cal.cell_boxes = [
            (
                int(round(x1 + bx1 / scale)),
                int(round(y1 + by1 / scale)),
                int(round(x1 + bx2 / scale)),
                int(round(y1 + by2 / scale)),
            )
            for bx1, by1, bx2, by2 in boxes
        ]
        log.debug("rutnat inpassat: %s", cal.cell_boxes)

    digits: list[DecodeResult] = []
    chars: list[str] = []

    for bx1, by1, bx2, by2 in boxes:
        cell = normalized[by1:by2, bx1:bx2]
        if cell.size == 0:
            digits.append(
                DecodeResult(char=" ", confidence=0.0, error=1.0, second_error=1.0, segment_values={})
            )
            chars.append(" ")
            continue
        result = decode_cell(cell)
        digits.append(result)
        chars.append(result.char)

    value = "".join(chars).strip()
    confident = [d.confidence for d in digits if not d.blank]
    confidence = float(min(confident)) if confident else 0.0

    return Reading(
        value=value,
        confidence=confidence,
        digits=digits,
        timestamp=timestamp,
        boxes=boxes,
        ok=bool(value),
    )


def consensus(
    readings: list[Reading],
    *,
    min_agreement: int = 3,
    min_confidence: float = 0.75,
) -> Consensus:
    """Rostar fram det varde som flest lasningar ar eniga om."""
    usable = [r for r in readings if r.ok and r.confidence >= min_confidence and r.value]
    if not usable:
        return Consensus(value=None, votes=0, total=len(readings), confidence=0.0)

    votes = Counter(r.value for r in usable)
    value, count = votes.most_common(1)[0]
    if count < min_agreement:
        return Consensus(value=None, votes=count, total=len(readings), confidence=0.0)

    agreeing = [r for r in usable if r.value == value]
    confidence = float(np.mean([r.confidence for r in agreeing]))

    return Consensus(value=value, votes=count, total=len(readings), confidence=confidence)


def save_debug(
    outdir: Path,
    stamp: str,
    image: np.ndarray,
    cal: Calibration,
    cfg: ReaderConfig,
    reading: Reading | None = None,
) -> Path:
    """Sparar ROI, trosklad bild och en markerad helbild for felsokning."""
    outdir.mkdir(parents=True, exist_ok=True)

    x1, y1, x2, y2 = cal.roi
    crop = image[y1:y2, x1:x2].copy()
    cv2.imwrite(str(outdir / f"{stamp}_roi.png"), crop)

    normalized, binary = preprocess(image, cal.roi, cfg)
    cv2.imwrite(str(outdir / f"{stamp}_bin.png"), binary)

    marked = image.copy()
    cv2.rectangle(marked, (x1, y1), (x2, y2), (0, 0, 255), 3)
    if reading:
        for bx1, by1, bx2, by2 in reading.boxes:
            rx1 = x1 + int(bx1 / (cfg.upscale or 1.0))
            ry1 = y1 + int(by1 / (cfg.upscale or 1.0))
            rx2 = x1 + int(bx2 / (cfg.upscale or 1.0))
            ry2 = y1 + int(by2 / (cfg.upscale or 1.0))
            cv2.rectangle(marked, (rx1, ry1), (rx2, ry2), (0, 255, 0), 2)
    cv2.imwrite(str(outdir / f"{stamp}_marked.png"), marked)

    return outdir
