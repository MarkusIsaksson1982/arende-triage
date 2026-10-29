"""Mask PII (personnummer, organisationsnummer, names, phone, e-mail) before sending text to the LLM.

Dataskydd: identifierare upptäcks och maskeras FÖRE reglerna, FÖRE model_fn och i
alla utdata (beslut, trace, granskningskö, audit, filer).

Ärendetexten är sjukvårdsdata enligt Patientdatalagen (2008:355). Personnummer,
namn, telefon, e-post och andra direkta identifierare ska aldrig lämna processen i
klartext - inte till regelmotorn, inte till modellen, inte till en fil.

Maskningen är ett HINDER, inte en lösning: orden är avsiktligt försiktiga, fångar
inte diagnoskoder eller fritext som råkar beskriva en person, och en fritextmaskering
kan övermaskera ett vanligt ord. Riktig användning kräver dessutom gallring,
åtkomstlogg och kryptering - se README.
"""
from __future__ import annotations

import re
from datetime import date

MASK = "[personnummer-maskat]"
NAMN_MASK = "[namn-maskat]"
TELEFON_MASK = "[telefon-maskat]"
EPOST_MASK = "[epost-maskat]"
IDENT_MASK = "[identifierare-maskat]"
ORGNR_MASK = "[organisationsnummer-maskat]"

MASKER = {"personnummer": MASK, "namn": NAMN_MASK, "telefon": TELEFON_MASK,
          "epost": EPOST_MASK, "identifierare": IDENT_MASK,
          "organisationsnummer": ORGNR_MASK}

# Ordning = prioritet. En kandidat som överlappar en redan accepterad träff
# hoppas över, så att t.ex. ett tioställigt telefonnummer inte maskas som
# personnummer. E-post går först eftersom den kan innehålla både siffror och @.
PRIORITET = ("epost", "telefon", "personnummer", "organisationsnummer", "identifierare", "namn")

# Syntetiska namn ur det fiktiva exempelmaterialet. Riktiga namn ska inte ligga i
# koden; listan är ett prov på hur namnlista + heuristik kan kombineras.
SYNTHETISKA_NAMN = ("Anna Svensson", "Erik Lindqvist", "Karin Ekström", "Lars Öberg",
                    "Fatima Hassan", "Bo Ek")

# 12 (YYYYMMDD-XXXX) eller 10 (YYMMDD-XXXX) siffror, med bindestreck, plustecken
# eller utan avgränsare. Avgränsarna skyddar mot att greda in i ett längre tal.
# 12 siffror bär århundradet; 10 tolkas med avgränsaren ('-' = under 100 år,
# '+' = 100 år eller mer) och kontrollsumman (Luhn) räknas på tio siffror.
_PERSONNUMMER = re.compile(r"(?<![\w-])(\d{4}|\d{2})(\d{2})(\d{2})([-+]?)(\d{4})(?![\w-])")
# Organisationsnummer (Swedish organisation number): NNNNNN-NNNN, optionally prefixed "16"
# (12-digit form used by Skatteverket). The third digit is 2 or higher, so it can never be a
# valid personnummer month; the tenth digit is a Luhn check digit over the ten digits.
_ORGNR = re.compile(r"(?<![\w-])(?:16)?(\d{2}[2-9]\d{3})-?(\d{4})(?![\w-])")
_EPOST = re.compile(r"(?<![\w-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])")
# Svenska telefonnummer: 070-174 06 05, 0701740605, +46 70 174 06 05 samt fasta nummer med riktnummer (08-, 0xx-).
# Områdesnumret måste se ut som ett svenskt område (07x mobil, 08/031/090 fast), och
# abonnentdelen måste gruppera sig som ett nummer. Det breda mönster som fanns
# tidigare tog varje siffersträng med tillräckligt många grupper, och fångade därför
# datum, belopp och ordningsnummer.
_TELEFON = re.compile(
    r"(?<![\w-])(?:\+46[\s.-]?|0)"
    r"(?:7[0-9][\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}"            # mobil: 070-174 06 05
    r"|[1-9]\d{0,2}[\s.-]?\d{2,4}[\s.-]?\d{2,4}[\s.-]?\d{2})"  # fast: riktnummer + abonnentnummer
    r"(?![\w-])")
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


def _luhn_ok(siffror: str) -> bool:
    """Luhn-kontroll (mod 10). Räknas på de tio siffrorna: YY + MM + DD + NNN + C."""
    total = 0
    for i, ch in enumerate(reversed(siffror)):
        siffra = int(ch)
        if i % 2:
            siffra = siffra * 2 - 9 if siffra > 4 else siffra * 2
        total += siffra
    return total % 10 == 0


def _år_från_två(år2: str, avgränsare: str, idag: date) -> int:
    """Årtalet i ett tioårigt personnummer - en tolkning, inte ett fakta.

    Två siffror bär inte med sig århundradet: '+' betyder 100 år eller mer
    (föregående århundrade), '-' och saknad avgränsare betyder yngre än 100 år,
    alltså det närmaste årtal som inte ligger i framtiden. Referensdatumet skickas
    in, så att ett test inte flyttar med sig när kalendern gör det.
    """
    århundra = (idag.year // 100 - (1 if avgränsare == "+" else 0)) * 100
    ar = århundra + int(år2)
    return ar - 100 if ar > idag.year else ar


def _datum_ok(ar: int, manad: int, dag: int, idag: date) -> bool:
    """Sparar ogiltiga datum så att ett ordningsnummer inte maskas som personnummer."""
    if not 1800 <= ar <= idag.year + 1 or not 1 <= manad <= 12:
        return False
    try:
        date(ar, manad, dag)
    except ValueError:
        return False
    return True


def _giltigt_personnummer(träff, idag: date) -> bool:
    """Datum och Luhn på en träff: 12 siffror bär århundradet, 10 tolkas med avgränsaren.

    Samordningsnummer har dagen +60 (61-91), så 850372-0016 är 1985-03-12. Luhn räknas
    på siffrorna som de står, alltså med den skrivna dagen.
    """
    år, manad, dag, avgränsare, kontroll = träff.groups()
    ar = int(år) if len(år) == 4 else _år_från_två(år, avgränsare, idag)
    dagnummer = int(dag)
    if not _datum_ok(ar, int(manad), dagnummer - 60 if 61 <= dagnummer <= 91 else dagnummer,
                     idag):
        return False
    return _luhn_ok(år[-2:] + manad + dag + kontroll)


def find_personnummer(text: str, idag: date | None = None) -> list[str]:
    """Alla personnummer i texten, i tur ordning.

    `idag` är referensdatumet för århundradet i tioåriget form (default: dagens datum),
    och skickas in utifrån så att testerna blir oberoende av kalendern.
    """
    idag = idag or date.today()
    return [m.group(0) for m in _PERSONNUMMER.finditer(text)
            if _giltigt_personnummer(m, idag)]


def _spann_personnummer(text, idag: date | None = None):
    idag = idag or date.today()
    for m in _PERSONNUMMER.finditer(text):
        if _giltigt_personnummer(m, idag):
            yield m.start(), m.end(), "personnummer", MASK


def _giltigt_organisationsnummer(träff) -> bool:
    return _luhn_ok(träff.group(1) + träff.group(2))


def find_organisationsnummer(text: str) -> list[str]:
    """Detect Swedish organisationsnummer (org number / company ID) with Luhn validation.

    Alla organisationsnummer i texten, i tur ordning. Både 556677-8899-formen och den
    tolvsiffriga 16-formen godtas; tredje siffran måste vara minst 2 och kontrollsiffran
    stämma (Luhn), annars är det bara ett tal.
    """
    return [m.group(0) for m in _ORGNR.finditer(text) if _giltigt_organisationsnummer(m)]


def _spann_organisationsnummer(text):
    for m in _ORGNR.finditer(text):
        if _giltigt_organisationsnummer(m):
            yield m.start(), m.end(), "organisationsnummer", ORGNR_MASK


def _spann_enkel(text, mönster, kategori, ersättning):
    for m in mönster.finditer(text):
        yield m.start(), m.end(), kategori, ersättning


def _spann_titel_namn(text):
    # Bara namnet maskas: titeln ("dr", "patient") är roll, inte identifierare.
    for m in _TITEL_NAMN.finditer(text):
        yield m.start(1), m.end(1), "namn", NAMN_MASK


def _kandidater(text, idag: date | None = None):
    """Identifierarkandidater i prioritetsordning - se PRIORITET."""
    return (_spann_enkel(text, _EPOST, "epost", EPOST_MASK),
            _spann_enkel(text, _TELEFON, "telefon", TELEFON_MASK),
            _spann_personnummer(text, idag),
            _spann_organisationsnummer(text),
            _spann_enkel(text, _IDENT, "identifierare", IDENT_MASK),
            _spann_titel_namn(text),
            _spann_enkel(text, _NAMNLISTA, "namn", NAMN_MASK))


def _krock(start: int, stop: int, träffar: list[tuple]) -> bool:
    """True om [start, stop) överlappar en redan accepterad träff."""
    return any(start < t[1] and stop > t[0] for t in träffar)


def mask_text(text, idag: date | None = None) -> tuple[str, list[str]]:
    """Maskera alla identifierare. Returnera (maskad text, kategorier i prioritetsordning).

    Kategorierna (t.ex. ["personnummer", "namn"]) går vidare till trace, så att det
    går att se att maskningen skett - utan att de råa värdena någonstans skrivs ut.
    `idag` är referensdatumet för århundradet i tioåriget personnummer.
    """
    if not isinstance(text, str):
        return text, []
    träffar: list[tuple[int, int, str, str]] = []
    for kandidater in _kandidater(text, idag):
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


def mask_note(text: str, idag: date | None = None) -> str:
    """Maskera en fritext (granskningsnotat) - används innan den sparas i audit."""
    return mask_text(text if isinstance(text, str) else "", idag)[0]


def sanitize(value, idag: date | None = None):
    """Maskera identifierare i alla strängar i ett beslut (djupt, utan att skapa cykler)."""
    if isinstance(value, str):
        return mask_text(value, idag)[0]
    if isinstance(value, dict):
        return {k: sanitize(v, idag) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, idag) for v in value]
    return value