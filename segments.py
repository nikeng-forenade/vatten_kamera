"""Avlasning av sjusegmentsiffror.

En display av sjusegmentstyp har sju staplar per siffra:

        aaa
      f     b
      f     b
        ggg
      e     c
      e     c
        ddd

I stallet for att kora OCR (som ar kansligt for suddighet) matar vi hur mycket
varje segment lyser och jamfor med de kanda monstren for 0-9. Det ar stabilt
aven nar siffrorna bara ar nagra tiotal pixlar hogt, och ger en konfidenststor
per siffra sa vi kan forkasta osakra lasningar.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Bitordning: a=1, b=2, c=4, d=8, e=16, f=32, g=64
SEGMENT_BITS: dict[str, int] = {
    "a": 1,
    "b": 2,
    "c": 4,
    "d": 8,
    "e": 16,
    "f": 32,
    "g": 64,
}

# Ritat monster (bitmask) for varje tecken.
DIGIT_MASKS: dict[str, int] = {
    "0": 1 + 2 + 4 + 8 + 16 + 32,          # 63
    "1": 2 + 4,                             # 6
    "2": 1 + 2 + 64 + 16 + 8,               # 91
    "3": 1 + 2 + 64 + 4 + 8,                # 79
    "4": 32 + 64 + 2 + 4,                   # 102
    "5": 1 + 32 + 64 + 4 + 8,               # 109
    "6": 1 + 32 + 64 + 16 + 4 + 8,          # 125
    "7": 1 + 2 + 4,                         # 7
    "8": 127,
    "9": 1 + 2 + 4 + 8 + 32 + 64,           # 111
    "-": 64,                                # bara mittensegmentet
}

# Segmentens utbredning som andel av siffercellens bredd/hojd.
#
# Fonstren ar matt pa den verkliga displayen med tools/probe_cell.py, som
# skriver ut cellen som en teckenkarta. Displayen gloder kraftigt: en nolla
# bestar av en tat vansterkolumn (x 0.09-0.40), ett hal (x 0.40-0.63, y
# 0.25-0.70) och en tat hogerkolumn (x 0.64-0.98). Halet ar alltsa bara ~23 %
# av bredden och sitter i mitten - det ar dar mittensegmentets fonster maste
# ligga. Lag det langre at vanster (som 0.28-0.45) hamnar det pa den vanstra
# stapeln, och da lases varje nolla som en atta.
#
# Sidofonstren (f/e och b/c) haller sig innanfor staplarna och en bit fran
# cellkanten: strax utanfor cellen sitter displayens kolon, och dess glod nar
# in over kanten och tander ett slakt segment.
SEGMENT_BOXES: dict[str, tuple[float, float, float, float]] = {
    "a": (0.18, 0.04, 0.45, 0.17),
    "f": (0.12, 0.24, 0.32, 0.38),
    "b": (0.70, 0.24, 0.88, 0.38),
    "g": (0.46, 0.45, 0.58, 0.55),
    "e": (0.12, 0.62, 0.32, 0.76),
    "c": (0.70, 0.62, 0.88, 0.76),
    "d": (0.18, 0.83, 0.45, 0.96),
}

# Vilken percentil som anvands inom varje fonster.
#
# 75:e percentilen ar mindre kanslig an medelvardet for ett par morka pixlar i
# kanten av ett tant segment. Mittensegmentet (g) ar undantaget: dar ar fragan om
# HALET i en nolla ar helt fyllt, och en glodande kant in i halet ska inte raknas
# som tant segment. Darfor mats aven den nedre delen av fonstret.
SEGMENT_PERCENTILE: dict[str, float] = {
    "a": 75.0,
    "f": 75.0,
    "b": 75.0,
    "g": 30.0,
    "e": 75.0,
    "c": 75.0,
    "d": 75.0,
}

# Hur mycket fel en siffra far ha for att anda godtas (RMSE i normaliserad skala).
MAX_ERROR = 0.42

# Minsta kontrast inom en cell for att den ska anses innehalla en siffra.
# En helt jamn yta (t.ex. pumphuset) skulle annars normaliseras till "alla
# segment lyser" och lasas som en atta med hog konfidens.
MIN_CONTRAST = 0.35


@dataclass(frozen=True)
class DecodeResult:
    """Resultatet for en enskild siffercell."""

    char: str
    confidence: float
    error: float
    second_error: float
    segment_values: dict[str, float]

    @property
    def blank(self) -> bool:
        return self.char == " "


def ideal_patterns() -> dict[str, np.ndarray]:
    """Returnerar varje teckens ideala 7-bitarsmönster som 1/0-vektor."""
    patterns: dict[str, np.ndarray] = {}
    for char, mask in DIGIT_MASKS.items():
        patterns[char] = np.array(
            [(mask >> bit) & 1 for bit in range(7)], dtype=np.float32
        )
    return patterns


IDEAL = ideal_patterns()
_SEGMENT_ORDER = ("a", "b", "c", "d", "e", "f", "g")


def segment_values(cell: np.ndarray) -> dict[str, float]:
    """Mater hur mycket varje segment lyser i en siffercell.

    `cell` ska vara en float-bild dar 1.0 = ljusast. Returnerar 0..1 per segment.
    """
    h, w = cell.shape[:2]
    values: dict[str, float] = {}

    for name, (fx1, fy1, fx2, fy2) in SEGMENT_BOXES.items():
        patch = segment_patch(cell, name)
        if patch is None:
            values[name] = 0.0
            continue
        values[name] = float(np.percentile(patch, SEGMENT_PERCENTILE.get(name, 75.0)))

    return values


def segment_patch(cell: np.ndarray, name: str) -> np.ndarray | None:
    """Utsnittet av cellen dar segmentet mats."""
    h, w = cell.shape[:2]
    fx1, fy1, fx2, fy2 = SEGMENT_BOXES[name]
    x1 = int(round(fx1 * w))
    x2 = max(x1 + 1, int(round(fx2 * w)))
    y1 = int(round(fy1 * h))
    y2 = max(y1 + 1, int(round(fy2 * h)))
    patch = cell[y1:y2, x1:x2]
    return patch if patch.size else None


def hole_is_filled(cell: np.ndarray, peak: float) -> float:
    """Hur stor del av halet som ar nastan lika ljust som cellens ljusaste segment.

    Fragan for mittensegmentet ar inte "hur ljust ar det har" utan "ar HALET i
    en nolla helt fyllt". Gloden runt segmenten varierar mellan bilderna och
    smetar in i halet, sa ett matt pa ljusnivan kan hamna over troskeln och gora
    en nolla till en atta. I stallet mats hur stor del av fonstret som ar nastan
    lika ljust som det ljusaste segmentet: en tаnd mittstapel ar mattad och ger
    ~1.0, ett hal som bara gloder ger en brakdel.
    """
    patch = segment_patch(cell, "g")
    if patch is None or peak <= 0.0:
        return 0.0
    return float(np.count_nonzero(patch >= 0.75 * peak)) / float(patch.size)


def values_to_vector(values: dict[str, float]) -> np.ndarray:
    return np.array([values.get(name, 0.0) for name in _SEGMENT_ORDER], dtype=np.float32)


def decode_cell(
    cell: np.ndarray,
    *,
    allow_blank: bool = True,
    blank_threshold: float = 0.28,
    min_contrast: float = MIN_CONTRAST,
) -> DecodeResult:
    """Tolkar en siffercell som en siffra 0-9 (eller blank)."""
    values = segment_values(cell)
    vector = values_to_vector(values)

    peak = float(vector.max())
    background = float(np.percentile(cell, 10)) if cell.size else 0.0
    contrast = peak - background

    # Cellen ar slackt om inget segment lyser namnvart.
    if peak < blank_threshold:
        return DecodeResult(
            char=" " if allow_blank else "?",
            confidence=1.0 - peak,
            error=0.0,
            second_error=1.0,
            segment_values=values,
        )

    # Jamn yta utan kontrast: har finns ingen siffra, och vi vet inte vad det
    # ar. Returnera ett tecken som aldrig kan rostas igenom.
    if contrast < min_contrast:
        return DecodeResult(
            char="?",
            confidence=0.0,
            error=1.0,
            second_error=1.0,
            segment_values=values,
        )

    # Normalisera sa att det ljusaste segmentet blir 1.0 - daligt ljus paverkar
    # da inte trosklingen.
    vector = np.clip(vector / peak, 0.0, 1.0)

    # Nu finns en verklig siffra i cellen, sa mittensegmentet avgors med
    # halmatet: ett hal som gloder svagt ar inte ett tant segment. Mattet ar
    # redan en andel av cellens ljusaste segment och skalas darfor inte med
    # `peak` som de andra.
    values["g"] = hole_is_filled(cell, peak)
    vector[_SEGMENT_ORDER.index("g")] = float(values["g"])

    scored: list[tuple[float, str]] = []
    for char, ideal in IDEAL.items():
        error = float(np.sqrt(np.mean((vector - ideal) ** 2)))
        scored.append((error, char))

    scored.sort()
    best_error, best_char = scored[0]
    second_error, _ = scored[1] if len(scored) > 1 else (1.0, "")

    if best_error > MAX_ERROR:
        confidence = 0.0
    else:
        # Konfidensen vaxer med hur mycket battre den basta tolkningen ar
        # an den nast basta, och med hur litet felet ar i sig.
        separation = (second_error - best_error) / max(second_error, 1e-6)
        confidence = float(max(0.0, (1.0 - best_error)) * separation)

    return DecodeResult(
        char=best_char,
        confidence=confidence,
        error=best_error,
        second_error=second_error,
        segment_values=values,
    )


# ---------------------------------------------------------------------------
# Syntetisk renderare - anvands av testerna for att verifiera avlasaren utan
# att vara beroende av kameran.
# ---------------------------------------------------------------------------

# Hur en riktig sjusegmentdisplay ser ut: de vagrata staplarna gar over hela
# sifferbredden och de lodrata ligger langs kanterna. Den har geometrin anvands
# bara av renderaren. Avlasaren mater med SEGMENT_BOXES, som medvetet ar smalare
# - poangen ar att de tva ska vara oberoende, sa att ett fel i matfonstren
# upptacks i stallet for att testet mater sin egen ritning.
RENDER_BOXES: dict[str, tuple[float, float, float, float]] = {
    "a": (0.08, 0.02, 0.92, 0.15),
    "f": (0.03, 0.17, 0.22, 0.46),
    "b": (0.78, 0.17, 0.97, 0.46),
    "g": (0.08, 0.46, 0.92, 0.59),
    "e": (0.03, 0.54, 0.22, 0.83),
    "c": (0.78, 0.54, 0.97, 0.83),
    "d": (0.08, 0.85, 0.92, 0.98),
}


def render_digit(
    char: str,
    width: int = 60,
    height: int = 110,
    *,
    blur: float = 0.0,
    noise: float = 0.0,
    background: float = 0.0,
    foreground: float = 1.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Ritar en siffra som en float-bild (1.0 = tande segment)."""
    canvas = np.full((height, width), background, dtype=np.float32)

    if char == " ":
        return canvas

    if char not in DIGIT_MASKS:
        raise ValueError(f"kan inte rita tecknet {char!r}")

    mask = DIGIT_MASKS[char]
    for index, name in enumerate(_SEGMENT_ORDER):
        if not (mask >> index) & 1:
            continue
        fx1, fy1, fx2, fy2 = RENDER_BOXES[name]
        x1, x2 = int(fx1 * width), int(np.ceil(fx2 * width))
        y1, y2 = int(fy1 * height), int(np.ceil(fy2 * height))
        canvas[y1:y2, x1:x2] = foreground

    if blur > 0:
        canvas = cv2.GaussianBlur(canvas, (0, 0), blur)

    if noise > 0:
        generator = rng or np.random.default_rng(0)
        canvas = canvas + generator.normal(0.0, noise, canvas.shape).astype(np.float32)

    return np.clip(canvas, 0.0, 1.0)


def render_number(
    text: str,
    *,
    digit_width: int = 60,
    digit_height: int = 110,
    gap: int = 8,
    **kwargs: object,
) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    """Ritar en hel sifferrad. Returnerar (bild, cellkoordinater)."""
    cells: list[np.ndarray] = []
    boxes: list[tuple[int, int, int, int]] = []
    x = 0

    for char in text:
        cell = render_digit(char, digit_width, digit_height, **kwargs)  # type: ignore[arg-type]
        cells.append(cell)
        boxes.append((x, 0, x + digit_width, digit_height))
        x += digit_width + gap

    total_width = max(1, x - gap)
    canvas = np.zeros((digit_height, total_width), dtype=np.float32)

    for cell, (bx1, _, bx2, _) in zip(cells, boxes):
        canvas[:, bx1:bx2] = cell

    return canvas, boxes
