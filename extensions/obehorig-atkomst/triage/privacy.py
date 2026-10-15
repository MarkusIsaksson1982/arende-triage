"""Dataskydd: identifierare upptäcks och maskeras FÖRE reglerna, FÖRE model_fn och i
alla utdata (beslut, trace, granskningskö, audit, filer).

Ärendetexten är sjukvårdsdata enligt Patientdatalagen (2008:355). Personnummer,
namn, telefon, e-post och andra direkta identifierare ska aldrig lämna processen i
klartext - inte till regelmotorn, inte till modellen, inte till en fil.

Maskningen är ett HINDER, inte en lösning: orden är avsiktligt försiktiga, fångar
inte samordningsnummer (dag 61-91), diagnoskoder eller fritext som råkar beskriva
en person, och en fritextmaskering kan övermaskera ett vanligt ord. Riktig
användning kräver dessutom gallring, åtkomstlogg och kryptering - se README.
"""
from __future__ import annotations

import re
from datetime import date

MASK = "[personnummer-maskat]"
NAMN_MASK = "[namn-maskat]"
TELEFON_MASK = "[telefon-maskat]"
EPOST_MASK = "[epost-maskat]"
IDENT_MASK = "[identifierare-maskat]"

MASKER = {"personnummer": MASK, "namn": NAMN_MASK, "telefon": TELEFON_MASK,
          "epost": EPOST_MASK, "identifierare": IDENT_MASK}

# Ordning = prioritet. En kandidat som överlappar en redan accepterad träff
# hoppas över, så att t.ex. ett tioställigt telefonnummer inte maskas som
# personnummer. E-post går först eftersom den kan innehålla både siffror och @.
PRIORITET = ("epost", "telefon", "personnummer", "identifierare", "namn")

# Syntetiska namn ur det fiktiva exempelmaterialet. Riktiga namn ska inte ligga i
# koden; listan är ett prov på hur namnlista + heuristik kan kombineras.
SYNTHETISKA_NAMN = ("Anna Svensson", "Erik Lindqvist", "Karin Ekström", "Lars Öberg",
                    "Fatima Hassan", "Bo Ek")

# 10 (YYMMDD-XXXX) eller 12 (YYYYMMDD-XXXX) siffror, med bindestreck, plustecken
# eller utan avgränsare. Avgränsarna skyddar mot att greda in i ett längre tal.
_PERSONNUMMER = re.compile(r"(?<![\w-])(\d{4}|\d{2})(\d{2})(\d{2})[-+]?(\d{4})(?![\w-])")
_EPOST = re.compile(r"(?<![\w-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])")
# Svenska nummer: 070-174 06 05, 0701740605, +46 70 174 06 05 (fiktionsserie).
_TELEFON = re.compile(r"(?<![\w-])(?:\+46[\s.-]?|0)\s*(?:\d{2,3}[\s.-]*)?\d{2,3}[\s.-]*\d{2,3}(?![\w-])")
# Andra direkta identifierare: patient-/dossier-/ärendenummer och liknande.
_IDENT = re.compile(r"(?<![\w-])(?:patient|dossier|ärende|avdelning|vårdenhet)"
                    r"\s*-?\s*(?:nummer|id|nr|no)\s*[:#]?\s*[A-Za-z0-9][A-Za-z0-9_-]{3,}",
                    re.IGNORECASE)
# Titel eller roll före ett namn: "dr Lindqvist", "patient Anna Svensson".
_TITLAR = (r"dr|docent|professor|överläkare|läkare|sjuksköterska|sjuksköterskor|"
           r"receptionist|fru|herr|barn|pojkar")
_NAMNKUPPLETER = (r"patient|patienten|pojken|flickan|granskare|granskat\s+av|"
                  r"inrapporterad\s+av|ansvarig")
_EFTERNAMN = r"[A-ZÅÄÖÉ][a-zåäöé]+(?:-[A-ZÅÄÖÉa-zåäöé]+)?"
_TITEL_NAMN = re.compile(r"(?<![\w-])(?:" + _TITLAR + r"|" + _NAMNKUPPLETER + r")\.?\s+"
                         r"(" + _EFTERNAMN + r"(?:\s+" + _EFTERNAMN + r")?)")
_NAMNLISTA = re.compile(r"(?<![\w-])(?:"
                        + "|".join(re.escape(n) for n in sorted(SYNTHETISKA_NAMN, key=len,
                                                                reverse=True))
                        + r")(?![\w-])", re.IGNORECASE)


def _datum_ok(aar: int, manad: int, dag: int) -> bool:
    """Sparar ogiltiga datum så att ett ordningsnummer inte maskas som personnummer."""
    ar = aar + 1900 if aar < 100 else aar
    if not 1800 <= ar <= date.today().year + 1 or not 1 <= manad <= 12:
        return False
    try:
        date(ar, manad, dag)
    except ValueError:
        return False
    return True


def find_personnummer(text: str) -> list[str]:
    """Alla personnummer i texten, i tur ordning."""
    return [m.group(0) for m in _PERSONNUMMER.finditer(text)
            if _datum_ok(int(m.group(1)[-2:]), int(m.group(2)), int(m.group(3)))]


def _spann_personnummer(text):
    for m in _PERSONNUMMER.finditer(text):
        if _datum_ok(int(m.group(1)[-2:]), int(m.group(2)), int(m.group(3))):
            yield m.start(), m.end(), "personnummer", MASK


def _spann_enkel(text, mönster, kategori, ersättning):
    for m in mönster.finditer(text):
        yield m.start(), m.end(), kategori, ersättning


def _spann_titel_namn(text):
    # Bara namnet maskas: titeln ("dr", "patient") är roll, inte identifierare.
    for m in _TITEL_NAMN.finditer(text):
        yield m.start(1), m.end(1), "namn", NAMN_MASK


def _kandidater(text):
    """Identifierarkandidater i prioritetsordning - se PRIORITET."""
    return (_spann_enkel(text, _EPOST, "epost", EPOST_MASK),
            _spann_enkel(text, _TELEFON, "telefon", TELEFON_MASK),
            _spann_personnummer(text),
            _spann_enkel(text, _IDENT, "identifierare", IDENT_MASK),
            _spann_titel_namn(text),
            _spann_enkel(text, _NAMNLISTA, "namn", NAMN_MASK))


def _krock(start: int, stop: int, träffar: list[tuple]) -> bool:
    """True om [start, stop) överlappar en redan accepterad träff."""
    return any(start < t[1] and stop > t[0] for t in träffar)


def mask_text(text) -> tuple[str, list[str]]:
    """Maskera alla identifierare. Returnera (maskad text, kategorier i prioritetsordning).

    Kategorierna (t.ex. ["personnummer", "namn"]) går vidare till trace, så att det
    går att se att maskningen skett - utan att de råa värdena någonstans skrivs ut.
    """
    if not isinstance(text, str):
        return text, []
    träffar: list[tuple[int, int, str, str]] = []
    for kandidater in _kandidater(text):
        for start, stopp, kategori, ersättning in kandidater:
            if _krock(start, stopp, träffar):
                continue
            träffar.append((start, stopp, kategori, ersättning))
    if not träffar:
        return text, []
    delar = []
    förra = 0
    for start, stopp, _kategori, ersättning in sorted(träffar):
        delar.append(text[förra:start])
        delar.append(ersättning)
        förra = stopp
    delar.append(text[förra:])
    kategorier = sorted({t[2] for t in träffar}, key=PRIORITET.index)
    return "".join(delar), kategorier


def mask_note(text: str) -> str:
    """Maskera en fritext (granskningsnotat) - används innan den sparas i audit."""
    return mask_text(text if isinstance(text, str) else "")[0]


def sanitize(value):
    """Maskera identifierare i alla strängar i ett beslut (djupt, utan att skapa cykler)."""
    if isinstance(value, str):
        return mask_text(value)[0]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value