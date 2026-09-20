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
from dataclasses import replace
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
    # Hur manga bilder lasningen vilar pa. Att lagga ihop lika bilder och tolka
    # medelvardesbilden tar bort brus, men da maste rosten vaga lika tungt som
    # antalet bilder - annars kan tio bilder av samma varde bara bli en rost.
    weight: int = 1

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


REFERENCE_FILE = "calibration_reference.png"

# Storsta forskjutning som vi tror pa. Ar bilden mer forskjuten an sa har
# kameran flyttats ordentligt och da ar de sparade cellerna anda fel.
MAX_ALIGN_SHIFT = 60.0
# phaseCorrelate:s svar. Lagt varde betyder att bilderna inte liknar varandra,
# t.ex. for att displayen visar nagot helt annat, och da ska vi inte flytta.
MIN_ALIGN_RESPONSE = 0.05


def estimate_shift(reference: np.ndarray, current: np.ndarray) -> tuple[float, float, float]:
    """Uppskattar hur mycket `current` ar forskjuten mot `reference`.

    Returnerar (dx, dy, svar). Bara translation, vilket racker nar kameran
    sitter fast och bara rubbas nagra pixel. Phase correlation ar snabb och
    itererar inte, till skillnad fran ECC.
    """
    if reference.shape != current.shape:
        return 0.0, 0.0, 0.0
    first = np.ascontiguousarray(reference, dtype=np.float32)
    second = np.ascontiguousarray(current, dtype=np.float32)
    # Ett fonster dampa kantartefakterna, annars ger de en falsk topp.
    window = cv2.createHanningWindow((first.shape[1], first.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(first, second, window)
    return float(dx), float(dy), float(response)


def load_reference(path: str | None) -> np.ndarray | None:
    """Laser referensbilden som float 0..1. None om den inte finns."""
    if not path:
        return None
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    return image.astype(np.float32) / 255.0


def save_reference(normalized: np.ndarray, path: str) -> None:
    """Sparar den normaliserade ROI-bilden som referens for inriktningen."""
    cv2.imwrite(str(path), np.clip(normalized * 255.0, 0.0, 255.0).astype("uint8"))


# ---------------------------------------------------------------------------
# Matning av siffercellerna (kalibreringen)
# ---------------------------------------------------------------------------


def half_max_box(gray: np.ndarray, box: Box) -> Box:
    """Drar at en klumps ruta till halvvardesbredden pa den glodande kanten.

    `gray` ar den normaliserade bilden (1.0 = ljusast). Halvvardesbredden ligger
    mitt i glodens kant och flyttar sig darfor mycket mindre an troskelns kant
    nar ljuset andras.
    """
    x1, y1, x2, y2 = box
    region = gray[y1:y2, x1:x2]
    if region.size == 0:
        return box

    def extent(profile: np.ndarray, offset: int) -> tuple[int, int]:
        peak = float(profile.max())
        if peak <= 0.0:
            return offset, offset + len(profile)
        lit = np.flatnonzero(profile >= 0.5 * peak)
        if lit.size == 0:
            return offset, offset + len(profile)
        return offset + int(lit[0]), offset + int(lit[-1]) + 1

    left, right = extent(region.max(axis=0), x1)
    top, bottom = extent(region.max(axis=1), y1)
    return left, top, right, bottom


def glyph_boxes(image: np.ndarray, roi: Box, cfg: ReaderConfig) -> list[Box]:
    """Sifferklumparna i en bild, i ROI-koordinater och uppskalade pixlar."""
    reader = replace(cfg, reference_file="")
    normalized, binary = preprocess(image, roi, reader)
    binary = clean_binary(binary)
    band = find_band(binary)
    band_height = band[3] - band[1]
    height, width = binary.shape[:2]

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    found: list[Box] = []
    for label in range(1, count):
        x, y, component_width, component_height, area = stats[label]
        if area < 20 or component_height < 0.45 * band_height:
            continue
        # En siffra som ror vid ROI:ts kant ar klippt. Da blir cellen for liten
        # och forskjuten, och lasningen blir fel utan att nagon siffra ser
        # konstig ut. Det ska upptacks direkt.
        if x <= 1 or y <= 1 or x + component_width >= width - 1 or y + component_height >= height - 1:
            log.warning(
                "en siffra ror vid ROI:ts kant och klipps - utoka CALIBRATION_ROI"
            )
        found.append(
            half_max_box(
                normalized,
                (int(x), int(y), int(x + component_width), int(y + component_height)),
            )
        )
    found.sort(key=lambda item: item[0])
    return found


def to_internal(boxes: list[Box], roi: Box, scale: float) -> list[Box]:
    """Helbildens koordinater -> ROI:t, uppskalat (samma rymd som klumparna)."""
    return [
        (
            int(round((x1 - roi[0]) * scale)),
            int(round((y1 - roi[1]) * scale)),
            int(round((x2 - roi[0]) * scale)),
            int(round((y2 - roi[1]) * scale)),
        )
        for x1, y1, x2, y2 in boxes
    ]


def to_absolute(boxes: list[Box], roi: Box, scale: float) -> list[Box]:
    """ROI:t, uppskalat -> helbildens koordinater."""
    return [
        (
            int(round(roi[0] + x1 / scale)),
            int(round(roi[1] + y1 / scale)),
            int(round(roi[0] + x2 / scale)),
            int(round(roi[1] + y2 / scale)),
        )
        for x1, y1, x2, y2 in boxes
    ]


def measure_cells(
    images: list[np.ndarray],
    roi: Box,
    cfg: ReaderConfig,
    digit_count: int,
    *,
    prior: list[Box] | None = None,
) -> tuple[list[Box], list[str]]:
    """Mater en cellruta per sifferposition. Returnerar (absoluta rutor, rapport).

    Kameran ser panelen snett, sa sifferraden lutar: sista siffran sitter
    nagra tiotal pixlar lagre i bilden an den forsta. Ett rutnat som antar att
    alla celler ligger pa samma hojd lagger matfonstren fel pa de hogra
    siffrorna, och da lases de fel (ofta som attor). Darfor mats varje position
    for sig - bade i x och i y.

    Varje bild tolkas for sig och varje sifferklump matas med halvvardesbredd.
    Klumparna laggs sedan ihop per position, sa att aven en position som bara
    visar en smal etta nagon gang far sin fulla bredd.

    `prior` ar sifferpositionerna fran en tidigare kalibrering (x-led ar
    palitligt darifran). Utan den maste bilderna visa alla positioner samtidigt.
    """
    scale = cfg.upscale if cfg.upscale else 1.0
    report: list[str] = []

    prior_internal = to_internal(prior, roi, scale) if prior else None
    if prior_internal and len(prior_internal) == digit_count:
        centers = [(box[0] + box[2]) / 2.0 for box in prior_internal]
    else:
        centers = []
        for image in images:
            for box in glyph_boxes(image, roi, cfg):
                centers.append((box[0] + box[2]) / 2.0)
        if len(centers) < 2:
            raise ReaderError("hittade inga siffror i bilderna")
        low, high = min(centers), max(centers)
        centers = [
            low + index * (high - low) / (digit_count - 1) for index in range(digit_count)
        ]

    pitch = (centers[-1] - centers[0]) / max(1, digit_count - 1)
    report.append("positionernas x : " + "  ".join(f"{center:.0f}" for center in centers))
    report.append(f"stigning        : {pitch / scale:.1f} px")

    per_slot: list[list[Box]] = [[] for _ in range(digit_count)]
    for image in images:
        for box in glyph_boxes(image, roi, cfg):
            center_x = (box[0] + box[2]) / 2.0
            index = int(np.argmin([abs(center_x - center) for center in centers]))
            if abs(center_x - centers[index]) > 0.6 * pitch:
                continue
            per_slot[index].append(box)

    for index, boxes in enumerate(per_slot, start=1):
        report.append(f"position {index}    : {len(boxes)} matningar")

    # Cellens storlek mats ur alla klumpar i serien. Den tas hogt ur
    # fordelningen (90:e percentilen) och inte som ett medianvarde: siffrorna i
    # den har displayen ar inte lika breda - en nolla ar ~95 px medan en sjua ar
    # ~80 px - och det ar den bredaste typen som visar hur bred cellen ar. En
    # cell som ar for smal lagger matfonstren innanfor segmenten, och en som ar
    # for bred lagger dem utanfor.
    all_widths = [box[2] - box[0] for boxes in per_slot for box in boxes]
    all_heights = [box[3] - box[1] for boxes in per_slot for box in boxes]
    if not all_widths or not all_heights:
        raise ReaderError("hittade inga siffror i bilderna")

    cell_w = min(float(np.percentile(all_widths, 90)), 1.15 * pitch)
    cell_h = float(np.percentile(all_heights, 90))
    report.append(f"cellstorlek     : {cell_w / scale:.0f} x {cell_h / scale:.0f} px")
    if max(all_widths) < 0.6 * pitch:
        report.append("  (bara ettor i serien - ta fler bilder sa att cellens bredd syns)")

    known = [index for index, boxes in enumerate(per_slot) if boxes]
    slope = None
    if len(known) >= 2:
        slope = np.polyfit(
            np.array(known, dtype=np.float64),
            np.array(
                [
                    float(np.median([(box[1] + box[3]) / 2.0 for box in per_slot[i]]))
                    for i in known
                ],
                dtype=np.float64,
            ),
            1,
        )

    cells: list[Box] = []
    for index in range(digit_count):
        boxes = per_slot[index]
        if boxes:
            center_y = float(np.median([(box[1] + box[3]) / 2.0 for box in boxes]))
            right = float(np.median([box[2] for box in boxes]))
            width = cell_w
        elif slope is not None:
            center_y = float(np.polyval(slope, index))
            right = centers[index] + cell_w / 2.0
            width = cell_w
            report.append(f"position {index + 1}    : ingen matning - hojden forlangd ur grannarna")
        else:
            raise ReaderError(f"position {index + 1} syntes inte i nagon bild")

        cells.append(
            (
                int(round(right - width)),
                int(round(center_y - cell_h / 2.0)),
                int(round(right)),
                int(round(center_y + cell_h / 2.0)),
            )
        )

    return to_absolute(cells, roi, scale), report


def load_cell_boxes(path: Path) -> list[Box] | None:
    """Sifferpositionerna ur en tidigare kalibrering.

    Anvands som "prior" vid en ny matning: lagen i x-led ar palitliga, sa de
    gor att positionerna hittas aven om bara nagra av dem lyser i serien.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        boxes = [tuple(int(value) for value in box) for box in data["cell_boxes"]]
    except (KeyError, ValueError, TypeError):
        return None
    return boxes or None


def draw_cells_overlay(image: np.ndarray, roi: Box, cells: list[Box], cfg: ReaderConfig) -> np.ndarray:
    """Ritar cellrutor och de sju matfonstren ovanpa bilden (en kontrollbild).

    Gron ruta = avlasaren tycker att segmentet lyser. Sitter rutorna pa
    segmenten ar geometrin ratt; hamnar de bredvid syns det direkt.
    """
    from segments import SEGMENT_BOXES, segment_values

    normalized, _binary = preprocess(image, roi, replace(cfg, reference_file=""))
    scale = cfg.upscale if cfg.upscale else 1.0
    canvas = cv2.cvtColor((normalized * 255).astype("uint8"), cv2.COLOR_GRAY2BGR)

    for ax1, ay1, ax2, ay2 in cells:
        cx1 = int(round((ax1 - roi[0]) * scale))
        cy1 = int(round((ay1 - roi[1]) * scale))
        cx2 = int(round((ax2 - roi[0]) * scale))
        cy2 = int(round((ay2 - roi[1]) * scale))
        if cx2 <= cx1 or cy2 <= cy1:
            continue
        values = segment_values(normalized[cy1:cy2, cx1:cx2])
        for name, (fx1, fy1, fx2, fy2) in SEGMENT_BOXES.items():
            sx1 = cx1 + int(round(fx1 * (cx2 - cx1)))
            sx2 = cx1 + max(1, int(round(fx2 * (cx2 - cx1))))
            sy1 = cy1 + int(round(fy1 * (cy2 - cy1)))
            sy2 = cy1 + max(1, int(round(fy2 * (cy2 - cy1))))
            colour = (0, 255, 0) if values.get(name, 0.0) > 0.5 else (0, 0, 255)
            cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), colour, 1)
        cv2.rectangle(canvas, (cx1, cy1), (cx2, cy2), (255, 255, 0), 1)

    return canvas


def read_image(
    image: np.ndarray,
    cal: Calibration,
    cfg: ReaderConfig,
    *,
    timestamp: float = 0.0,
) -> Reading:
    """Laser displayen i en bild och returnerar varde + konfidens."""
    normalized, binary = preprocess(image, cal.roi, cfg)

    # Rikta in bilden mot referensen innan cellerna anvands. Kameran sitter
    # fast, men vibrationer och temperaturdrift flyttar bilden nagra pixel over
    # tid, och da hamnar de sju matfonstren fel. Referensen sparas vid
    # kalibreringen och ar samma normaliserade ROI-bild som vi jamfor med har.
    offset = (0, 0)
    reference = load_reference(getattr(cfg, "reference_file", ""))
    if reference is not None and reference.shape == normalized.shape:
        dx, dy, response = estimate_shift(reference, normalized)
        if response >= MIN_ALIGN_RESPONSE and (abs(dx) > 0.5 or abs(dy) > 0.5):
            if abs(dx) <= MAX_ALIGN_SHIFT and abs(dy) <= MAX_ALIGN_SHIFT:
                offset = (int(round(dx)), int(round(dy)))
                log.debug("bilden ar forskjuten %s mot referensen - cellerna foljer med", offset)
            else:
                log.warning(
                    "bilden ar forskjuten %.0f, %.0f px mot referensen, mer an gransen"
                    " %.0f px - laser med de sparade cellerna",
                    dx,
                    dy,
                    MAX_ALIGN_SHIFT,
                )

    if cal.cell_boxes:
        scale = cfg.upscale if cfg.upscale else 1.0
        x1, y1 = cal.roi[0], cal.roi[1]
        image_height, image_width = normalized.shape[:2]
        boxes = []
        for bx1, by1, bx2, by2 in cal.cell_boxes:
            cx1 = int(round((bx1 - x1) * scale))
            cy1 = int(round((by1 - y1) * scale))
            cx2 = int(round((bx2 - x1) * scale))
            cy2 = int(round((by2 - y1) * scale))
            # En cell som ligger utanfor bilden far aldrig bli en negativ
            # skiva - da vander indexeringen och cellen blir tom utan att nagon
            # siffra ser konstig ut. Klipp mot bilden och saga till.
            if cx2 <= 0 or cy2 <= 0 or cx1 >= image_width or cy1 >= image_height:
                log.warning(
                    "cellen %s,%s,%s,%s ligger utanfor bilden - kontrollera CALIBRATION_ROI",
                    bx1, by1, bx2, by2,
                )
            boxes.append(
                (
                    max(0, min(image_width, cx1)),
                    max(0, min(image_height, cy1)),
                    max(0, min(image_width, cx2)),
                    max(0, min(image_height, cy2)),
                )
            )
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

    if offset != (0, 0):
        boxes = [
            (bx1 + offset[0], by1 + offset[1], bx2 + offset[0], by2 + offset[1])
            for bx1, by1, bx2, by2 in boxes
        ]

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

    # Displayen kan inte visa 888. Blir alla siffror attor beror det pa att
    # gloden runt segmenten slagit igen halen i en nolla - inte pa att vardet
    # verkligen ar 888. Att publicera 8.88 vore varre an att inte publicera
    # nagot alls, sa en sadan lasning forkastas.
    if getattr(cfg, "reject_all_eights", False) and digits:
        visible = [digit.char for digit in digits if not digit.blank]
        if visible and all(char == "8" for char in visible):
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
    """Rostar fram det varde som flest lasningar ar eniga om.

    Varje lasning vager sa tungt som antalet bilder bakom den, sa att en grupp
    av lika bilder raknas som lika manga roster.
    """
    usable = [r for r in readings if r.ok and r.confidence >= min_confidence and r.value]
    total = sum(max(1, r.weight) for r in readings)
    if not usable:
        return Consensus(value=None, votes=0, total=total, confidence=0.0, decimals=decimals)

    votes: Counter[str] = Counter()
    for reading in usable:
        votes[reading.value] += max(1, reading.weight)

    value, count = votes.most_common(1)[0]
    if count < min_agreement:
        return Consensus(
            value=None, votes=count, total=total, confidence=0.0, decimals=decimals
        )

    agreeing = [r for r in usable if r.value == value]
    confidence = float(np.mean([r.confidence for r in agreeing]))

    return Consensus(
        value=value, votes=count, total=total, confidence=confidence, decimals=decimals
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
        reading.weight = len(indices)
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
