"""Deterministiska regler: ingen modell, ingen slump - samma text ger samma poäng.

Tre områden plus övrigt, med skarpa gränser mellan dem:
  felaktig_inloggning        autentisering (lösenord, användarnamn, session)
  obehorig_atkomst           behörighet och åtkomstloggning - inte ett inloggningsfel
  avvikelse_i_patientjournal  dokumentation, dosering, signering, rättelse, fel patient

Två av etiketterna går ALLTID till mänsklig granskning, även när reglerna var
entydiga: journalföring (inkl. fel patient/förväxling) och behörighetsfrågor får
aldrig tystas bort av en nyckelordsmatchning. Bara en entydig autentiseringsetikett
kan beslutas automatiskt. Ett fall med svag topp eller liten marginal är inte
"övrigt" utan OSÄKERT, och skickas vidare i kedjan.

Verktygsnamn och autentiseringsmetoder väger bara tillsammans med sin kontext, se
KONTEKST nedan.
"""
from __future__ import annotations

import re

from .schema import display_label

LOGIN = "felaktig_inloggning"
ACCESS = "obehorig_atkomst"
JOURNAL = "avvikelse_i_patientjournal"
OTHER = "ovrigt"

# Etiketter som alltid kräver en människa, oavsett hur tydliga reglerna var.
ALWAYS_REVIEW = (JOURNAL, ACCESS)

# Ordet "journal" i ett verktygsnamn ("journalsystemet", "journalportalen") är inte
# journalföring. Därför lista vi suffixen vi menar i stället för \w*, och låter
# sammansättningar som "Patientjournalen" och "journalföring" räknas.
JOURNAL_ORD = (r"\b(?:patient)?journal"
               r"(?:en|er|ering|föring|förts|förda|anteckning|anteckningar|rad|rader"
               r"|post|poster|referens)?\b")

# (etikett, vikt, mönster). Vikten speglar hur specifik trappen är.
PATTERNS = [
    (LOGIN, 3, r"felaktigt\s+l[öo]senord"),
    (LOGIN, 3, r"ogiltigt\s+(?:anv[äa]ndarnamn|l[öo]senord)"),
    (LOGIN, 3, r"kunde\s+inte\s+logga\s+in"),
    (LOGIN, 3, r"inloggningen\s+misslyckades"),
    (LOGIN, 3, r"(?:blir|blev|blivit)\s+(?:utloggad|utloggat)\s+(?:direkt|genast|omedelbart)"),
    (LOGIN, 2, r"dubbel\s+inloggning"),
    (LOGIN, 2, r"sessionen\s+(?:g[åa]r\s+ut|avslutades|l[öo]ps\s+ut)"),
    (LOGIN, 2, r"sp[äa]rrad\s+(?:lista|vy|meny)"),             # stängd vy, inte behörighet
    (LOGIN, 2, r"medarbetar(?:uppdrag|inloggning)"),
    (LOGIN, 1, r"\blogga?\s+in\b"),      # ensam: under tröskeln, skickas till modellen
    (ACCESS, 3, r"inloggad\w*\s+samtidigt"),
    (ACCESS, 3, r"[åa]tkomstlogg"),
    (ACCESS, 2, r"[åa]tkomst\s+(?:nekad|avslagen)"),
    (ACCESS, 2, r"sp[äa]rrad\s+(?:beh[öo]righet|beh[öo]righetsgrupp|roll)"),
    (ACCESS, 2, r"beh[öo]righet\w*\s+(?:[äa]r\s+)?sp[äa]rrad"),
    (ACCESS, 2, r"beh[öo]righet\s+saknas"),
    (ACCESS, 2, r"obeh[öo]rig\s+[åa]tkomst"),
    (ACCESS, 2, r"annan\s+(?:anv[äa]ndare|person)\s+(?:kunde|kan)\s+se"),
    (ACCESS, 2, r"logguppf[öo]ljning"),
    (JOURNAL, 3, r"dokumenterat\s+felaktig"),
    (JOURNAL, 3, r"felaktig\w*\s+(?:doser\w+|registrerat|inf[öo]rd)"),
    (JOURNAL, 3, r"avvikelse\s+(?:i\s+journal|rapport)"),
    (JOURNAL, 3, r"avvikelse\s+registreras"),
    (JOURNAL, 3, r"(?:fel|felaktig\w*|f[öo]rv[äa]xlad\w*)\s+(?:patient|person)\b"),
    (JOURNAL, 3, r"f[öo]rv[äa]xling"),
    (JOURNAL, 3, r"sammanbland\w*\s+(?:av\s+)?(?:två\s+)?(?:journal|patient)"),
    (JOURNAL, 3, r"(?:r[äa]ttelse|r[äa]ttat)\s+i\s+journal"),
    (JOURNAL, 2, r"journal(?:ering|f[öo]ring)"),
    (JOURNAL, 2, r"anteckningen\s+(?:saknar|st[åa]r)\s"),
    (JOURNAL, 2, r"saknas\s+i\s+journalen"),
    (JOURNAL, 2, r"korrigering\s+av\s+journal"),
    (JOURNAL, 2, r"signerat\s+(?:felaktigt|efter\s+hand)"),
    (JOURNAL, 2, r"efterhands?[äa]ndring"),
    (JOURNAL, 1, JOURNAL_ORD),
    (JOURNAL, 1, r"\bavvikelse\w*"),
]

# Mönster som kräver FLERA signaler samtidigt. Ett verktygsnamn eller en
# autentiseringsmetod ensam får aldrig avgöra en etikett: SITHS, BankID och HSA
# väger därför bara 1 poäng och bara i inloggningssammanhang (logga in, inloggning,
# kort, legitimation). Utan kontext träffar de inte alls, och tillsammans kan de
# aldrig väga över en tydlig journal- eller åtkomstträff.
KONTEKST = [
    (LOGIN, 1, "SITHS/BankID/HSA i inloggningskontext",
     # ordgräns på metoden: "bankidsystemet" och "sithsportalen" är verktyg
     r"\b(?:siths|bankid)\b|\bhsa(?:-|\s)?id\b",
     # kontexten får däremot ha böjningsformer ("kortet", "inloggningen")
     r"\b(?:logga\s+in|loggat\s+in|loggade\s+in|loggar\s+in|inloggning\w*|kort(?:et)?"
     r"|legitimer\w*|autentiser\w*)"),
]

# Undantagsmönster: en patient som ringer/samtalar ska inte trigga felaktig inloggning.
# Ordgräns efter "samtal" krävs så att "samtalsunderlaget" inte tas med, och ett
# maskat namn ("Patient [namn-maskat] ringer") får inte dölja undantaget. Mönster med
# vikt 3 (dokumenterat felaktig dos, fel patient) överlever ändå - de talar om ett
# registrerat fel, inte om ett samtal.
UNDANTAG = re.compile(r"patienten\s+(?:\[[a-zåäö-]+\]\s+)?(?:ringer|samtalar|samtal)\b",
                      re.IGNORECASE)
UNDANTAG_SLIPPER_OVER = 3   # vikter under detta uteblir när undantaget slår till

MIN_SCORE = 2.0    # en enda vag signal räcker inte för att trigga modellen
MIN_MARGIN = 2.0   # för liten skillnad mellan kategorier = inte en entydig etikett


def normalize(text: str) -> str:
    return " ".join(text.split())


def _kontext_träffar(haystack: str) -> list[dict]:
    """Träffar från mönster som kräver flera signaler samtidigt."""
    träffar = []
    for label, weight, namn, *krav in KONTEKST:
        funna = [re.search(mönster, haystack, re.IGNORECASE) for mönster in krav]
        if all(funna):
            träffar.append({"label": label, "phrase": funna[0].group(0).strip(),
                            "weight": float(weight), "rule": namn})
    return träffar


def decide_rules(text: str) -> dict:
    """Poäng per kategori. En entydig etikett får confidence, annars label=None."""
    haystack = normalize(text)
    scores: dict[str, float] = {}
    hits: list[dict] = []
    undantag = bool(UNDANTAG.search(haystack))
    for label, weight, pattern in PATTERNS:
        if undantag and weight < UNDANTAG_SLIPPER_OVER:
            continue
        found = re.search(pattern, haystack, re.IGNORECASE)
        if found:
            scores[label] = scores.get(label, 0.0) + weight
            hits.append({"label": label, "phrase": found.group(0).strip(), "weight": float(weight)})
    for hit in _kontext_träffar(haystack) if not undantag else []:
        scores[hit["label"]] = scores.get(hit["label"], 0.0) + hit["weight"]
        hits.append(hit)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranked:
        return {"label": None, "topp": None, "confident": False, "confidence": 0.0,
                "margin": 0.0, "scores": {}, "hits": [], "undantag": undantag}
    topp, top_score = ranked[0]
    margin = top_score - (ranked[1][1] if len(ranked) > 1 else 0.0)
    # Lopp mellan kategorier ger ingen etikett: toppen är då godtyckligt vald, och
    # det är precis den etiketten en granskare eller modellforslag skulle jämföras mot.
    confident = top_score >= MIN_SCORE and margin >= MIN_MARGIN
    return {"label": topp if confident else None, "topp": topp, "confident": confident,
            "margin": round(margin, 2),
            "confidence": round(min(0.95, 0.6 + 0.15 * top_score), 2) if confident else 0.0,
            "scores": scores, "hits": hits, "undantag": undantag}


def beskriv_scores(rr: dict) -> str:
    """Regelpoängen som mänskligt läsbar text: svenska etiketter, fallande ordning."""
    ordnad = sorted(rr["scores"].items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join("{} {}".format(display_label(etikett), poang) for etikett, poang in ordnad) \
        or "inga träffar"