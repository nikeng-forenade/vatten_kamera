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
    # Fyran pa DEN HAR displayen har en hoger stapel som nar anda upp i
    # overkanten (se tools/check_digits.py och de tva fyror som mottes
    # 2026-09-21: a-fonstret matt till 1.00 i bada). Med ett idealt
    # sjusegmentmonster (a slackt) blev fyran i stallet en nia: bada har tva fel
    # - en fyra ett tant a, en nia ett tant d - och da blev felen lika stora och
    # konfidensen 0.00. Det ar bottenstrecket (d) som skiljer dem at, och det
    # mats med fyllnadsmatet.
    "4": 1 + 32 + 64 + 2 + 4,               # a, b, c, f, g
    "5": 1 + 32 + 64 + 4 + 8,               # 109
    "6": 1 + 32 + 64 + 16 + 4 + 8,          # 125
    "7": 1 + 2 + 4,                         # 7
    "8": 127,
    # Nian har ett fullt bottenstreck (mat 2026-09-20 pa vardet 0.92: botten-
    # strecket lyser over hela den vanstra halvan) och skiljs fran en atta av
    # det NEDRE VANSTRA segmentet, som ar slackt pa en nia. Det ar darfor e
    # mats som "fyllt" nedan - gloden fran bottenstrecket och mittstapeln
    # smetar annars in i e-fonstret och gor nian till en atta.
    "9": 1 + 2 + 4 + 8 + 32 + 64,          # a, b, c, d, f, g
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
    "f": (0.12, 0.22, 0.32, 0.34),
    # Ovra hogra fonstret sitter en bit NERAT. Displayens sexa och femma har en
    # hake: oversta strecket böjer av nedat i det ovre hogra hornet. Med fonstret
    # hogt upp (y 0.22-0.34) mattes den haken in - en sexa fick b=0.61 och lases
    # som en atta (b=0.61 ar narmare 1 an 0). En atta, en nolla och en fyra har
    # hela hogerkolumnen tand hela vagen, sa de klarar ett fonster langre ner.
    # Matt 2026-09-21 med tools/check_digits.py.
    "b": (0.70, 0.30, 0.88, 0.42),
    "g": (0.46, 0.45, 0.58, 0.55),
    # Nedre vanstra (e) sitter en bit hogre upp an vad segmentet gor pa ett
    # idealt sjusegment: bottenstreckets glod nar upp i den nedre delen av
    # fonstret, och en nia (som har slackt e) fick da 43 % fyllt - nian och
    # attan kom sa nara varandra att nian bara fick konfidens 0.22. Flyttat upp
    # till y 0.58-0.70 sitter fonstret dar en sexa ar mattad och gloden fran
    # bottenstrecket inte nar. Matt 2026-09-21 med tools/check_digits.py.
    "e": (0.12, 0.58, 0.32, 0.70),
    "c": (0.70, 0.62, 0.88, 0.76),
    # Bottenstrecket mats i cellens VANSTRA halva. Hogra halvan gar inte: dar
    # lyser bade ettans stapel och sjuan, som har en fot nedtill, sa fonstret
    # tands av dem (och en sjua lastes som en trea).
    "d": (0.18, 0.83, 0.45, 0.96),
}

# Vilken percentil som anvands inom varje fonster.
#
# 75:e percentilen ar mindre kanslig an medelvardet for ett par morka pixlar i
# kanten av ett tant segment.
#
# Tre av segmenten mats inte alls med percentil, utan med FYLLNAD (se
# filled_share): mittensegmentet (g), bottenstrecket (d) och det nedre vanstra
# (e). De ligger alla inklamda mellan tva tande staplar, och gloden fran
# grannarna smetar in over dem. En glodande springa kan da ha hog ljusniva utan
# att vara ett tant segment.
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


def filled_share(cell: np.ndarray, name: str, peak: float) -> float:
    """Hur stor del av segmentets fonster som ar nastan lika ljust som det ljusaste.

    Fragan for ett segment som ligger inklamt mellan tva tande staplar ar inte
    "hur ljust ar det har" utan "ar segmentet FYLLT". Gloden runt grannarna
    varierar mellan bilderna och smetar in over ett slackt segment, sa ett matt
    pa ljusnivan kan hamna over troskeln - och da blir en nolla en atta, en nia
    en atta och en sjua en trea. I stallet mats hur stor del av fonstret som ar
    nastan lika ljust som det ljusaste segmentet: en tаnd stapel ar mattad och
    ger ~1.0, en springa som bara gloder ger en brakdel.
    """
    patch = segment_patch(cell, name)
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

    # Nu finns en verklig siffra i cellen, sa de segment som ligger inklamda
    # mellan tande staplar - eller som far glod fran en granne - avgors med
    # fyllnadsmatet i stallet for ljusnivan.
    #
    # Ovra hogra (b) hor hit: displayens sexa och femma har en hake som böjer av
    # nedat i det hornet. En tand hake gloder starkt men fyller bara en del av
    # fonstret, medan en riktig b-stapel (atta, nolla, fyra, etta, sjua) ar
    # mattad over hela fonstret. Matt 2026-09-21: en sexa fick b=0.56 pa
    # ljusniva - narmare 1 an 0 - och lases som en atta.
    #
    # Ovra vanstra (f) far samma glod av oversta streckets vanstra ande, som
    # lutar nedat i det hornet. En trea fick f=0.26 och en tvaa f=0.25 pa
    # ljusniva. Trean och nian skiljs bara av f, sa gloden gjorde dem tvetydiga
    # (trean fick konfidens 0.33, och MIN_CONFIDENCE ar 0.35).
    for name in ("g", "d", "e", "b", "f"):
        values[name] = filled_share(cell, name, peak)
        vector[_SEGMENT_ORDER.index(name)] = float(values[name])

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
# sifferbredden och de lodrata ar tjocka. Den har geometrin anvands bara av
# renderaren. Avlasaren mater med SEGMENT_BOXES, som har andra matt - poangen ar
# att de tva ska vara oberoende, sa att ett fel i matfonstren upptacks i stallet
# for att testet mater sin egen ritning.
#
# Staplarna ar medvetet tjocka: displayen har tata, ~30 % breda kolumner (mat
# 2026-09-20: vansterkolumnen x 0.09-0.40), och fyllnadsmatet nedan fragar om
# matfonstret ar FYLLT. En tunn stapel som inte tacker fonstret skulle ge en
# tand nolla ett halvfyllt e och gora nollan till en nia.
RENDER_BOXES: dict[str, tuple[float, float, float, float]] = {
    "a": (0.10, 0.03, 0.90, 0.16),
    "f": (0.07, 0.18, 0.37, 0.45),
    "b": (0.63, 0.18, 0.93, 0.45),
    "g": (0.10, 0.46, 0.90, 0.60),
    "e": (0.07, 0.56, 0.37, 0.84),
    "c": (0.63, 0.56, 0.93, 0.84),
    "d": (0.10, 0.82, 0.90, 0.99),
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
