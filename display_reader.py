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
    # Antal decimaler i vardet, sa att "122" blir 1.22.
    decimals: int = 0

    @property
    def numeric(self) -> float | None:
        """Vardet som tal. "122" med tva decimaler blir 1.22."""
        digits_only = "".join(ch for ch in self.value if ch.isdigit())
        if not digits_only:
            return None
        return int(digits_only) / (10.0**self.decimals)


@dataclass
class Consensus:
    """Resultatet av en majoritetsrostning over flera bilder."""

    value: str | None
    votes: int
    total: int
    confidence: float
    decimals: int = 0

    @property
    def numeric(self) -> float | None:
        digits_only = "".join(ch for ch in self.value if ch.isdigit()) if self.value else ""
        if not digits_only:
            return None
        return int(digits_only) / (10.0**self.decimals)

    @property
    def ok(self) -> bool:
        return self.value is not None


# ---------------------------------------------------------------------------
# Bildbehandling
# ---------------------------------------------------------------------------


def pick_channel(image: np.ndarray, mode: str) -> tuple[np.ndarray, str]:
    """Gor om en fargbild till graaskala och tala om vilken kanal som valdes.

    En rod LED-display lyser starkast i den roda kanalen - men gloden runt
    siffrorna gors ocksa av rodt ljus. Ar gloden problemet ar gron- eller
    blokanalen battre, for dar lyser siffrorna men inte gloden. Darfor mater vi
    vilken kanal som har storst skillnad mellan ljust och morkt.
    """
    if image.ndim == 2:
        return image.astype(np.float32), "gray"

    if mode == "gray":
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32), "gray"

    index = {"b": 0, "g": 1, "r": 2}.get(mode)
    if index is not None:
        return image[:, :, index].astype(np.float32), mode

    best_channel: np.ndarray | None = None
    best_name = "gray"
    best_spread = -1.0
    for name, channel_index in (("b", 0), ("g", 1), ("r", 2)):
        if channel_index >= image.shape[2]:
            continue
        channel = image[:, :, channel_index].astype(np.float32)
        spread = float(np.percentile(channel, 99.5) - np.percentile(channel, 10))
        if spread > best_spread:
            best_spread = spread
            best_channel = channel
            best_name = name

    if best_channel is None:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32), "gray"
    return best_channel, best_name


def channel_spreads(image: np.ndarray) -> dict[str, float]:
    """Hur stor ar skillnaden mellan ljust och morkt i varje kanal?"""
    if image.ndim == 2:
        return {"gray": float(np.percentile(image, 99.5) - np.percentile(image, 10))}

    spreads: dict[str, float] = {}
    for name, index in (("b", 0), ("g", 1), ("r", 2)):
        if index < image.shape[2]:
            channel = image[:, :, index].astype(np.float32)
            spreads[name] = float(np.percentile(channel, 99.5) - np.percentile(channel, 10))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    spreads["gray"] = float(np.percentile(gray, 99.5) - np.percentile(gray, 10))
    return spreads


def preprocess(image: np.ndarray, roi: Box, cfg: ReaderConfig) -> tuple[np.ndarray, np.ndarray]:
    """Returnerar (normaliserad float-bild 0..1, trosklad binarbild) for ROI:t."""
    x1, y1, x2, y2 = roi
    h, w = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        raise ReaderError(f"ROI {roi} ligger utanfor bilden {w}x{h}")

    crop = image[y1:y2, x1:x2]
    gray, _channel_name = pick_channel(crop, getattr(cfg, "channel", "auto"))

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

    # Rensa bort decimalpunkten och stora ljusa ytor redan har, sa att alla steg
    # som utgar fran den binara bilden ser samma sak.
    binary = clean_binary(binary)

    return normalized, binary


def clean_binary(
    binary: np.ndarray,
    min_height_ratio: float = 0.20,
    min_width_ratio: float = 0.12,
    max_area_fraction: float = 0.30,
) -> np.ndarray:
    """Rensar den trosklade bilden sa att bara siffersegmenten blir kvar.

    Tva slags skrap stor tolken:
      * decimalpunkten - den lyser som en siffra men ar liten, och gor att
        sifferbandet blir bredare an siffrorna sa delningen inte stämmer,
      * pumphuset och andra stora ljusa ytor - de ar for stora for att vara
        siffror och drar ut bandet over hela bilden.

    En prick maste vara liten i BADE bredd och hojd for att rensas. De vagrata
    segmenten i en siffra ar ocksa lave men breda, och maste vara kvar.
    """
    if binary.size == 0:
        return binary

    height, width = binary.shape[:2]
    roi_area = float(height * width)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    mask = np.zeros_like(binary)

    for label in range(1, count):
        _x, _y, component_width, component_height, area = stats[label]
        if area < 6:
            continue
        if area > max_area_fraction * roi_area:
            continue
        if component_width > 0.85 * width and component_height > 0.85 * height:
            continue
        if (
            component_height < min_height_ratio * height
            and component_width < min_height_ratio * height
        ):
            continue
        mask[labels == label] = 255

    return mask


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

    # Decimalpunkten lyser men ar mycket lavere an siffrorna, och ska inte
    # vara med och bestamma var sifferbandet borjar och slutar.
    binary = clean_binary(binary)

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

    def score_of(boxes: list[Box]) -> float:
        return score_boxes(normalized, binary, band, boxes)

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


def score_boxes(normalized: np.ndarray, binary: np.ndarray, band: Box, boxes: list[Box]) -> float:
    """Hur bra forklarar detta rutnat bilden? Hogre ar battre.

    Vi vager samman tre saker:
      * hur saker tolken ar pa varje siffra,
      * att cellernas granser inte skar genom tta segment,
      * att inga tta pixlar lamnas utanfor rutnatet.
    """
    bx1, by1, bx2, by2 = band
    band_rows = binary[by1:by2, :]
    lit_per_column = np.count_nonzero(band_rows, axis=0).astype(np.float64)
    total_lit = float(lit_per_column.sum())
    width = lit_per_column.shape[0]

    scores: list[float] = []
    for cx1, cy1, cx2, cy2 in boxes:
        cell = normalized[cy1:cy2, cx1:cx2]
        if cell.size == 0:
            return -3.0
        result = decode_cell(cell)
        # En tom cell ar tillaten (en slackt siffra) men ska vaga latt.
        scores.append(0.15 if result.blank else result.confidence)

    penalty = 0.6 * _boundary_penalty(binary, boxes)

    if total_lit > 0:
        covered = np.zeros(width, dtype=bool)
        for cx1, _, cx2, _ in boxes:
            covered[max(0, cx1) : min(width, cx2)] = True
        penalty += 0.6 * (float(lit_per_column[~covered].sum()) / total_lit)

    return float(np.mean(scores)) - penalty


def detect_cells_by_blobs(binary: np.ndarray, band: Box, digit_count: int) -> list[Box] | None:
    """Hittar siffrorna som egna klumpar i bilden.

    Segmenten inne i en siffra sitter tat ihop, medan avstandet mellan tva
    siffror ar mycket storre. Fyller vi igen de sma glappen smalter varje siffra
    ihop till en klump - och da far vi siffrornas exakta lagen utan att behova
    anta att de sitter pa jamna avstand.

    Det sistnamnda ar viktigt: displayen har ett kolon mellan tva av siffrorna,
    sa avstanden ar inte jamna. Ett jamnt rutnat hamnar da fel.

    Decimalpunkten och kolonprickarna ar sma och filtreras bort pa hojden.
    """
    x1, y1, x2, y2 = band
    band_height = y2 - y1
    if band_height < 8 or x2 <= x1:
        return None

    region = binary[y1:y2, x1:x2]
    if region.size == 0:
        return None

    # Fyll igen glappen mellan segmenten i en siffra, men inte mellan siffror.
    radius = max(1, int(0.08 * band_height)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius, radius))
    merged = cv2.morphologyEx(region, cv2.MORPH_CLOSE, kernel)

    count, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)

    blobs: list[tuple[int, int, int, int]] = []
    for label in range(1, count):
        bx, by, width, height, area = stats[label]
        if area < 20:
            continue
        # Prickar (decimalpunkt, kolon) ar lave - siffror ar hoga.
        if height < 0.45 * band_height:
            continue
        blobs.append((int(bx), int(by), int(width), int(height)))

    if len(blobs) != digit_count:
        return None

    blobs.sort(key=lambda blob: blob[0])

    cells: list[Box] = []
    for bx, by, width, height in blobs:
        cells.append((x1 + bx, y1 + by, x1 + bx + width, y1 + by + height))

    return cells


def detect_cells(normalized: np.ndarray, binary: np.ndarray, band: Box, digit_count: int) -> list[Box] | None:
    """Hittar siffercellerna genom att utga fran mellanrummen mellan siffrorna.

    Mellan tva siffror pa en sjusegmentsdisplay finns en mork lucka. Vi delar
    bandet jamnt och knapper sedan varje intern cellgrans till den morkaste
    kolumnen i narheten - alltsa precis dar luckan sitter. Varje siffra far
    sedan en tat cell runt sina egna tta pixlar, sa att segmentens samplingsrutor
    hamnar pa segmenten och inte pa tomrum.

    Att leta upp luckorna ar sakerrare an att gissa cellbredder: en felbredd pa
    en pixel vandrar ivag over hela raden och gor att sista siffran hamnar fel.
    """
    x1, y1, x2, y2 = band
    if x2 - x1 < digit_count * 3 or y2 <= y1:
        return None

    # Enklaste vagen forst: hitta varje siffra som en egen klump. Den metoden
    # klarar att siffrorna inte sitter pa jamna avstand, vilket de inte gor nar
    # displayen har ett kolon mellan tva av dem.
    by_blobs = detect_cells_by_blobs(binary, band, digit_count)
    if by_blobs is not None:
        return by_blobs

    # Decimalpunkten ska inte vara med och bestamma delningen.
    digits_only = clean_binary(binary)
    rows = digits_only[y1:y2, :]
    if rows.size == 0 or np.count_nonzero(rows) == 0:
        rows = binary[y1:y2, :]
    if rows.size == 0:
        return None

    # Hur manga tta pixlar finns i varje kolumn?
    profile = rows.sum(axis=0).astype(np.float64)

    pitch = (x2 - x1) / float(digit_count)
    bounds = [float(x1 + index * pitch) for index in range(digit_count + 1)]
    search = max(1, int(pitch * 0.35))

    for index in range(1, digit_count):
        low = int(max(0, bounds[index] - search))
        high = int(min(len(profile), bounds[index] + search + 1))
        if high <= low:
            continue
        bounds[index] = float(low + int(np.argmin(profile[low:high])))

    cells: list[Box] = []
    for index in range(digit_count):
        slot_left = int(np.floor(bounds[index]))
        slot_right = int(np.ceil(bounds[index + 1]))
        slot_left = max(0, min(slot_left, len(profile) - 1))
        slot_right = max(slot_left + 1, min(slot_right, len(profile)))
        if slot_right <= slot_left:
            return None

        sub = digits_only[y1:y2, slot_left:slot_right]
        columns = np.flatnonzero(sub.sum(axis=0) > 0)
        if columns.size == 0:
            # Siffran ar slackt just nu - behall hela platsen.
            cells.append((slot_left, y1, slot_right, y2))
            continue

        left = slot_left + int(columns[0])
        right = slot_left + int(columns[-1]) + 1
        if right <= left:
            right = left + 1
        cells.append((left, y1, right, y2))

    # Rimlighetskontroll: cellerna ska ha liknande bredd och tacka det mesta av
    # det tta innehaller.
    widths = [cell[2] - cell[0] for cell in cells]
    if min(widths) < 2:
        return None
    median_width = float(np.median(widths))
    if max(widths) > 2.0 * median_width:
        return None

    lit_total = float(np.count_nonzero(digits_only[y1:y2, x1:x2]))
    if lit_total > 0:
        covered = 0.0
        for cell in cells:
            covered += float(np.count_nonzero(digits_only[y1:y2, cell[0] : cell[2]]))
        if covered < 0.85 * lit_total:
            return None

    return cells


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
        # Rutnatet passas in en gang och sparas i kalibreringen. Sedan laser vi
        # med de sparade cellerna, sa att sjalva lasningen ar snabb och inte
        # behover gissa om indelningen.
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

    # Visar displayen alltid alla siffror (som i 0.50) sa betyder en slackt
    # position att rutnatet hamnat fel. Da ska lasningen inte kunna rostas fram.
    if getattr(cfg, "require_all_digits", True) and any(d.blank for d in digits):
        confidence = 0.0

    # Tidsskarmarna (klockan 15:54 och spoltiden 02:00) anvander alla fyra
    # positionerna, medan vardeskarmarna (vatten kvar 1.25 och flode nu 0.15)
    # lamnar den FORSTA slackt: vardet star i position 2-4 med decimalpunkten
    # efter position 2. Genom att krava en slackt forsta position kan en
    # tidsskarm aldrig rostas fram som ett varde, annars skulle klockan 15:54
    # publiceras som 5.54 och spoltiden 02:00 som 2.00.
    if getattr(cfg, "require_blank_first", False) and digits and not digits[0].blank:
        confidence = 0.0

    return Reading(
        value=value,
        confidence=confidence,
        digits=digits,
        timestamp=timestamp,
        boxes=boxes,
        ok=bool(value),
        decimals=getattr(cfg, "decimals", 0),
    )


def consensus(
    readings: list[Reading],
    *,
    min_agreement: int = 3,
    min_confidence: float = 0.75,
    decimals: int = 0,
) -> Consensus:
    """Rostar fram det varde som flest lasningar ar eniga om."""
    usable = [r for r in readings if r.ok and r.confidence >= min_confidence and r.value]
    if not usable:
        return Consensus(value=None, votes=0, total=len(readings), confidence=0.0, decimals=decimals)

    votes = Counter(r.value for r in usable)
    value, count = votes.most_common(1)[0]
    if count < min_agreement:
        return Consensus(
            value=None, votes=count, total=len(readings), confidence=0.0, decimals=decimals
        )

    agreeing = [r for r in usable if r.value == value]
    confidence = float(np.mean([r.confidence for r in agreeing]))

    return Consensus(
        value=value, votes=count, total=len(readings), confidence=confidence, decimals=decimals
    )


# ---------------------------------------------------------------------------
# Medelvardesbildning over flera bilder
# ---------------------------------------------------------------------------
#
# Siffrorna ar sma, sa en enstaka bild ar kanslig for sensorns brus. Vardet star
# stilla i 10-12 sekunder, sa vi kan lagga ihop de bilder som visar samma sak
# och tolka summan i stallet. Bruset ar slumpmassigt och vags ut, medan
# siffrorna ar oforandrade och star kvar. Det ger betydligt sakrare lasningar
# av sma siffror.


def crop_roi(image: np.ndarray, roi: Box, channel: str = "auto") -> np.ndarray:
    """Skalar av ROI:t fran en bild och gor det till en graaskala i float."""
    x1, y1, x2, y2 = roi
    h, w = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        raise ReaderError(f"ROI {roi} ligger utanfor bilden {w}x{h}")

    return pick_channel(image[y1:y2, x1:x2], channel)[0]


def group_similar(crops: list[np.ndarray], threshold: float = 8.0) -> list[list[int]]:
    """Delar in bilderna i grupper dar gruppens bilder visar samma sak.

    Bilderna jamfors med den senaste gruppens medelvarde. Nar displayen byter
    varde blir skillnaden stor och en ny grupp startas. Pa sa vis blandas aldrig
    tva olika varden ihop.

    Jamforelsen gors pa utjamnade bilder. Sensorbrus ar slumpmassigt och forsvinner
    vid utjamningen, medan ett vardebyte andrar ett helt segment och star kvar.
    Utan det skulle bruset ensamt kunna se ut som ett vardebyte, och da blir
    varje bild sin egen grupp utan att nagon medelvardesbildning sker.
    """
    groups: list[list[int]] = []

    # Utjamning for jamforelsen - inte for sjalva tolkningen.
    smoothed = [
        cv2.GaussianBlur(crop, (0, 0), 1.5) if crop.ndim == 2 else crop for crop in crops
    ]

    for index, crop in enumerate(smoothed):
        if not groups or crop.shape != smoothed[groups[-1][0]].shape:
            groups.append([index])
            continue

        current = (
            smoothed[groups[-1][0]]
            if len(groups[-1]) == 1
            else np.mean([smoothed[i] for i in groups[-1]], axis=0)
        )
        difference = float(np.mean(np.abs(crop - current)))
        if difference <= threshold:
            groups[-1].append(index)
        else:
            groups.append([index])

    return groups


def average_crops(crops: list[np.ndarray], indices: list[int]) -> np.ndarray:
    """Medelvardet av de valda bilderna."""
    if len(indices) == 1:
        return crops[indices[0]]
    return np.mean([crops[i] for i in indices], axis=0)


def image_from_crop(crop: np.ndarray, roi: Box) -> np.ndarray:
    """Bygger en bild dar utsnittet ligger pa ROI:ts plats, sa att kalibreringen passar."""
    _, _, x2, y2 = roi
    canvas = np.zeros((max(1, y2), max(1, x2), 3), dtype=np.uint8)
    canvas[roi[1] : roi[1] + crop.shape[0], roi[0] : roi[0] + crop.shape[1]] = cv2.cvtColor(
        np.clip(crop, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR
    )
    return canvas


def read_crops(
    crops: list[np.ndarray],
    cal: Calibration,
    cfg: ReaderConfig,
    *,
    threshold: float = 8.0,
    min_frames: int = 1,
) -> list[Reading]:
    """Tolkar en serie utsnitt, med medelvardesbildning inom varje grupp."""
    if not crops:
        return []

    readings: list[Reading] = []

    for indices in group_similar(crops, threshold):
        if len(indices) < min_frames:
            continue
        averaged = average_crops(crops, indices)
        image = image_from_crop(averaged, cal.roi)
        reading = read_image(image, cal, cfg, timestamp=float(len(indices)))
        readings.append(reading)

    return readings


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
