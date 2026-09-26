"""Tester for den nattliga korningen.

Det som provas har ar vilka lasningar som far ligga till grund for rostningen:
vardet som ska ut visas strax efter att pumpen slar om till spolning, och resten
av fonstret visar displayen sina andra sidor.

Kor:  .venv/Scripts/python.exe -m pytest tests -q
"""

from __future__ import annotations

from display_reader import consensus
from pipeline import (
    _page_value_av,
    flow_targets,
    has_recharge_page,
    page_kind,
    readings_after_recharge,
    voting_targets,
)
from segments import DecodeResult


def cell(char: str) -> DecodeResult:
    return DecodeResult(
        char=char, confidence=0.9 if char != " " else 0.9, error=0.0, second_error=1.0, segment_values={}
    )


def reading(value: str, *, digits: list[str], confidence: float, bilder: int = 1) -> object:
    from display_reader import Reading

    result = Reading(
        value=value,
        confidence=confidence,
        digits=[cell(char) for char in digits],
        timestamp=0.0,
        boxes=[],
    )
    # Antalet bilder gruppen vilar pa - det ar sa mycket en sida vager i rostningen.
    result.weight = bilder
    return result


def spoltid(bilder: int = 1) -> object:
    """Displayens 02:00-sida: alla fyra positioner tands."""
    return reading("0200", digits=list("0200"), confidence=0.0, bilder=bilder)


def varde(text: str, confidence: float = 0.7, bilder: int = 1) -> object:
    """En vardesida: vardet i position 2-4, den forsta slackt."""
    return reading(text, digits=[" ", *text], confidence=confidence, bilder=bilder)


def toning(text: str, confidence: float = 0.5) -> object:
    """En enstaka bild mitt i ett sidbyte: ett varde som inte finns pa displayen."""
    return varde(text, confidence, bilder=1)


def annan_tidssida(text: str, bilder: int = 1) -> object:
    return reading(text, digits=list(text), confidence=0.0, bilder=bilder)


def flodes_sida(text: str = "000", confidence: float = 0.75, bilder: int = 7) -> object:
    """Displayens flodessida.

    Den ser likadan ut som vardesidan for avlasaren - vardet i position 2-4 med
    den forsta slackt - och bada kan lasas med hog konfidens. Det ar bara
    ordningen i varvet som skiljer dem at. Matt 2026-09-26: vardet '037' i 6-8
    bilder, sedan '000' i 7 bilder, sedan klockan '1223', sedan spolttiden.
    """
    return varde(text, confidence, bilder=bilder)


def test_flodet_ar_sidan_efter_vardet() -> None:
    """Varvet ar klockan -> 02:00 -> vardet -> flodet, och flodet lases sist."""
    svep = [
        varde("037", bilder=6),
        flodes_sida("000"),
        annan_tidssida("1223", bilder=8),
        spoltid(bilder=7),
        varde("037", bilder=8),
        flodes_sida("000", bilder=4),
    ]

    sida, note = flow_targets(svep, "037")

    assert note == ""
    assert {item.value for item in sida} == {"000"}
    # Flodet far aldrig bli vardet: det som rostas fram ar fortfarande 037.
    assert _page_value_av(voting_targets(svep)[0]) == "037"


def test_annat_flode_an_noll_lases_ocksa() -> None:
    """Kor pumpen star det ett varde pa flodessidan - da ska det med."""
    svep = [
        annan_tidssida("1223", bilder=6),
        spoltid(bilder=6),
        varde("035", bilder=7),
        flodes_sida("124", bilder=6),
        annan_tidssida("1224", bilder=6),
    ]

    sida, note = flow_targets(svep, "035")

    assert note == ""
    assert {item.value for item in sida} == {"124"}


def test_inget_flode_nar_sidan_inte_syntes() -> None:
    """Hellre inget flode an ett gissat: ett felaktigt flode ser ut som lackage."""
    svep = [varde("037", bilder=8), annan_tidssida("1223", bilder=8), spoltid(bilder=7)]

    sida, note = flow_targets(svep, "037")

    assert sida == []
    assert "ingen flodessida" in note


def test_inget_flode_nar_sidan_ar_olasbar() -> None:
    """En grupp pa manga bilder som inte gick att lasa ar en sida - inte ett flode."""
    svep = [
        varde("037", bilder=8),
        varde("000", confidence=0.0, bilder=15),
        annan_tidssida("1223", bilder=6),
    ]

    sida, note = flow_targets(svep, "037")

    assert sida == []
    assert note


def test_page_kind_skiljer_tidssida_fran_vardesida() -> None:
    assert page_kind(spoltid()) == "tid"
    assert page_kind(annan_tidssida("1832")) == "tid"
    assert page_kind(varde("116")) == "varde"


def test_valjer_vardet_som_kommer_efter_spolttidssidan() -> None:
    readings = [
        varde("037"),  # vardesida som visas innan spolningen
        spoltid(),
        varde("116"),  # vardet efter 02:00 - det som ska ut
        annan_tidssida("0800"),
    ]
    chosen = readings_after_recharge(readings)

    assert [item.value for item in chosen] == ["116"]


def test_hoppar_over_en_tidssida_mellan_spolttid_och_varde() -> None:
    readings = [spoltid(), annan_tidssida("0800"), varde("116")]
    assert [item.value for item in readings_after_recharge(readings)] == ["116"]


def test_utan_spolttidssida_rostas_inget_bort() -> None:
    # Sa lange displayen inte visat 02:00 far hela korningen ligga till grund.
    readings = [varde("037"), varde("116")]
    assert readings_after_recharge(readings) == []


def test_tva_omgangar_ger_tva_roster_for_samma_varde() -> None:
    # Displayen vaxlar i cykler: spolttid -> varde -> annan sida -> spolttid ...
    readings = [
        spoltid(),
        varde("116"),
        annan_tidssida("0800"),
        spoltid(),
        varde("116"),
        varde("037"),
    ]
    chosen = readings_after_recharge(readings)

    assert [item.value for item in chosen] == ["116", "116"]


def test_samma_vardesida_raknas_bara_en_gang() -> None:
    # Flera spolttidssidor i rad pekar pa samma vardesida. Utan det skyddet
    # skulle en enda grupp bilder kunna vaga hur tungt som helst.
    readings = [spoltid(), spoltid(), spoltid(), varde("116")]
    chosen = readings_after_recharge(readings)

    assert [item.value for item in chosen] == ["116"]


def test_flodet_efter_vardet_rostas_inte() -> None:
    # Sidvarvet pa displayen ar klockan -> spolttiden -> vardet -> flodet.
    # Vardet (0.91) och flodet (0.00) ser likadana ut for avlasaren, sa bara
    # ordningen i varvet skiljer dem at.
    readings = [
        spoltid(),
        varde("091"),
        varde("000"),
        annan_tidssida("2050"),
        spoltid(),
        varde("091"),
        varde("000"),
    ]
    assert [item.value for item in readings_after_recharge(readings)] == ["091", "091"]


def test_spolttidssidan_sist_ger_ingen_rostning() -> None:
    # Fonstret tog slut mitt i sidvarvet: da finns ingen vardesida efter
    # spolttidssidan, och flodet far inte publiceras som vardet. Bättre att
    # inget varde kommer ut an att 0.00 kommer ut som om det vore vardet.
    readings = [varde("091"), varde("000"), spoltid()]
    target, note = voting_targets(readings)

    assert target == []
    assert "skilja" in note
    assert has_recharge_page(readings)


def test_utan_spolttidssida_rostas_hela_fonstret() -> None:
    # En manuell lasning pa dagen kan sakna spolttidssidan. Da finns ingen
    # ordning att ga efter, och hela fonstret far ligga till grund.
    readings = [varde("091"), varde("091"), varde("091")]
    target, note = voting_targets(readings)

    assert target == readings
    assert note == ""
    assert not has_recharge_page(readings)


def test_vardesidan_efter_spolttiden_valjs_av_korningen() -> None:
    readings = [spoltid(), varde("091"), varde("000")]
    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["091"]
    assert note == ""


def test_hela_vardesidan_vags_samman_aven_om_den_ar_delad() -> None:
    # Displayen tonar in sidan, sa grupperingen delar den i flera grupper: nagra
    # enstaka bilder forst och en stor grupp sedan. Alla bilder pa vardesidan
    # maste vaga, annars kan en grupp pa en enda bild bli den enda rosten - och
    # da nar rostningen inte upp i MIN_AGREEMENT och inget varde publiceras.
    readings = [
        spoltid(),
        varde("090", 0.6),
        varde("090", 0.6),
        varde("090", 0.6),
        varde("090"),
        varde("000"),
        varde("000"),
    ]
    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["090", "090", "090", "090"]
    assert note == ""


def test_toningen_fore_vardesidan_kapar_inte_svepet() -> None:
    # Korningen 21:01, ordagrant: spolttiden, en enstaka bild som lastes som
    # '0891', och sedan 14 bilder pa 0.91. Den gamla rostningen tog den forsta
    # lasningen som "sidan", och da tog svepet slut dar - vardet tappades och
    # bara flodet blev kvar. En sida vilar pa manga bilder; en toning gor det inte.
    readings = [
        varde("037", bilder=7),  # vardesidan fore spolningen
        spoltid(bilder=14),
        annan_tidssida("0891"),  # toningen mellan tva sidor
        varde("091", 0.72, bilder=14),
        varde("000", bilder=15),  # flodet efter vardet
    ]
    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["091"]
    assert target[0].weight == 14
    assert note == ""


def test_en_toning_som_lasts_som_vardesida_ar_inte_en_sida() -> None:
    # Samma sak nar toningen lastes som en vardesida (forsta positionen slackt):
    # den vilar pa en bild och far inte bestamma vardet.
    readings = [
        spoltid(bilder=15),
        toning("099"),
        varde("091", 0.8, bilder=14),
        varde("000", bilder=15),
    ]
    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["091"]
    assert note == ""


def test_hela_sidan_vager_nar_flimret_delat_den() -> None:
    # Korningen 21:05: vardesidan lag i grupper om 1+1+1+1+15 bilder. Bara den
    # storsta gruppen fick ligga till grund, och da nadde rostningen inte upp i
    # MIN_AGREEMENT - trots att 19 bilder visade samma varde.
    readings = [
        spoltid(bilder=15),
        varde("090", 0.64, bilder=1),
        varde("090", 0.64, bilder=1),
        varde("090", 0.65, bilder=1),
        varde("090", 0.65, bilder=1),
        varde("090", 0.64, bilder=15),
        varde("000", bilder=6),
        annan_tidssida("2100", bilder=7),
    ]
    target, note = voting_targets(readings)

    assert sum(item.weight for item in target) == 19
    assert note == ""

    result = consensus(target, min_agreement=3, min_confidence=0.35, decimals=2)
    assert result.value == "090"
    assert result.numeric == 0.90
    assert result.votes == 19


def test_flodessidan_efter_vardet_kommer_inte_med() -> None:
    # Sahar sag korningen 21:08 ut: spolttiden delad i manga grupper, vardet i en
    # stor grupp, och flodet efterat. Bara vardet far rostas fram.
    readings = [
        annan_tidssida("2103", bilder=15),
        *[spoltid() for _ in range(15)],
        varde("090", 0.79, bilder=20),  # vardet - 20 bilder, sidan star stilla
        varde("000", bilder=15),  # flodet
        annan_tidssida("2104", bilder=7),
    ]
    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["090"]
    assert target[0].weight == 20
    assert note == ""


def test_olasbar_vardesida_ger_inget_varde() -> None:
    # Korningen 17:14: vardesidan stod stilla i 15 bilder men kunde inte lasas -
    # siffran 4 skiljdes inte fran 9 och sidan fick konfidens 0.00. Da fortsatte
    # rostningen till flodessidan och publicerade 0.00 som om det vore vardet.
    # En sida som star stilla ar vardesidan, aven nar den inte kan lasas, sa
    # nagot varde ska inte publiceras.
    readings = [
        spoltid(bilder=15),
        reading("084", digits=[" ", "0", "8", "4"], confidence=0.0, bilder=15),
        varde("000", bilder=15),
    ]
    target, note = voting_targets(readings)

    assert target == []
    assert "lasa" in note
    assert readings_after_recharge(readings) == []


def test_enstaka_olasbar_bild_stoppar_inte_rostningen() -> None:
    # En toning kan ocksa bli en bild som inte gar att tolka. En enstaka bild ar
    # inte en sida, och den far inte hindra vardet som kommer efterat.
    readings = [
        spoltid(bilder=15),
        reading("084", digits=[" ", "0", "8", "4"], confidence=0.0),
        varde("091", 0.72, bilder=14),
        varde("000", bilder=15),
    ]
    target, note = voting_targets(readings)

    assert [item.value for item in target] == ["091"]
    assert note == ""
