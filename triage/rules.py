"""Swedish support ticket triage rules: deterministic rules run before the model.

Deterministiska regler: ingen modell, ingen slump - samma text ger samma poäng.

Tre etiketter, med skarpa gränser mellan dem:
  felaktig_inloggning         autentisering och åtkomst till egen plats (lösenord,
                              användarnamn, session, delad eller stulen inloggning,
                              behörighet som saknas eller är spärrad)
  avvikelse_i_patientjournal   dokumentation, dosering, signering, rättelse, fel
                              patient - alltså fel i journalföringen
  ovrigt                      inget av ovan, se "Osäkra fall" i README

Treetikettig variant: åtkomst- och behörighetsärenden kan inte få en egen etikett.
Någon annan som har läst patientens uppgifter är inte ett journaldokumentationsfel
utan ett informationssäkerhets- och åtkomstkontrollärende enligt Patientdatalagen
(2008:355) 4 kap. (behörighet, åtkomst och logguppföljning): etiketten blir ovrigt,
men ärendet får aldrig besvaras automatiskt - det går till behörighetsansvarig, se
ROUTING_HINT och REVIEW_MÖNSTER. Åtkomst nekad trots rätt behörighet, spärrad
behörighet och delad/stulen session är i stället "jag kommer inte in"-ärenden, alltså
inloggning - men de ska aldrig besvaras automatiskt, se REVIEW_MÖNSTER.

Journalavvikelse går ALLTID till mänsklig granskning - fel patient och förväxling får
aldrig tystas bort - och det gäller även de inloggning- och åtkomstärenden som är
delar i ett större händelseförlopp (se REVIEW_MÖNSTER). Bara en entydig
autentiseringsetikett utan sådant underlag kan beslutas automatiskt. Ett fall med svag
topp eller liten marginal är inte "övrigt" utan OSÄKERT, och skickas vidare i kedjan.

Verktygsnamn och autentiseringsmetoder väger bara tillsammans med sin kontext, se
KONTEKST nedan.
"""
from __future__ import annotations

import re

from .schema import BEHÖRIGHETSANSVARIG, display_label

LOGIN = "felaktig_inloggning"
JOURNAL = "avvikelse_i_patientjournal"
OTHER = "ovrigt"

# Etiketter som alltid kräver en människa, oavsett hur tydliga reglerna var.
ALWAYS_REVIEW = (JOURNAL,)

# Mönster för "jag kommer inte in"-ärenden: delad/stulen session och behörighet
# som saknas eller är spärrad. Etiketten blir felaktig_inloggning (autentisering och
# åtkomst till egen plats), men ärendet ska till behörighetsansvarig - en
# lösenordsåterställning löser det inte - och granskas därför alltid av en människa.
# Samma strängar återanvänds i PATTERNS, så att vikten och granskningsbehovet inte
# kan glida isär.
SAMTIDIG = r"inloggad\w*\s+samtidigt"
DUBBEL = r"dubbel\s+inloggning"
STULEN_SESSION = r"session\w*\s+(?:stulen|delad)"
NEKAD = r"[åa]tkomst\s+(?:nekad|avslagen)"
BEHÖRIGHET_SPÄRRAD = r"sp[äa]rrad\s+(?:beh[öo]righet|beh[öo]righetsgrupp|roll)"
BEHÖRIGHET_MARKERAD = r"beh[öo]righet\w*\s+(?:[äa]r\s+)?sp[äa]rrad"
BEHÖRIGHET_SAKNAS = r"beh[öo]righet\s+saknas"

# Obehörig åtkomst till andras uppgifter: obehörig läsning av journal, åtkomstlogg och
# logguppföljning. Enligt Patientdatalagen (2008:355) 4 kap. gäller behörighet, åtkomst
# och logguppföljning - det är informationssäkerhet och åtkomstkontroll, inte
# journalföring, så mönstren väger mot OTHER. Etiketten blir ovrigt, men ärendet
# besvaras aldrig automatiskt (se REVIEW_MÖNSTER) och hänvisas till behörighetsansvarig
# (se ROUTING_HINT). Samma strängar återanvänds på båda ställena, så att vikt,
# granskningsbehov och hänvisning inte kan glida isär.
ÅTKOMST_LOGG = r"[åa]tkomstlogg"
UTAN_BEHÖRIGHET = r"utan\s+beh[öo]righet"
OBEHÖRIG_ÅTKOMST = r"obeh[öo]rig\s+[åa]tkomst"
ANNAN_SER = r"annan\s+(?:anv[äa]ndare|person)\s+(?:kunde|kan)\s+se"
LOGGUPPFÖLJNING = r"logguppf[öo]ljning"
ÅTKOMSTMÖNSTER = (ÅTKOMST_LOGG, UTAN_BEHÖRIGHET, OBEHÖRIG_ÅTKOMST, ANNAN_SER,
                  LOGGUPPFÖLJNING)

# Vem ett åtkomst- eller behörighetsärende ska handläggas av. Hänvisningen ligger i
# beslutet så att granskningskön pekar ut mottagaren, inte bara säger "granska".
ROUTING_HINT = BEHÖRIGHETSANSVARIG

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
    (LOGIN, 3, SAMTIDIG),                     # delad session, inte ett journalärende
    (LOGIN, 2, DUBBEL),
    (LOGIN, 2, STULEN_SESSION),
    (LOGIN, 2, r"sessionen\s+(?:g[åa]r\s+ut|avslutades|l[öo]ps\s+ut)"),
    (LOGIN, 2, r"sp[äa]rrad\s+(?:lista|vy|meny)"),             # stängd vy, inte behörighet
    (LOGIN, 2, r"medarbetar(?:uppdrag|inloggning)"),
    (LOGIN, 2, NEKAD),                        # kommer inte in: behörighet, inte journalföring
    (LOGIN, 2, BEHÖRIGHET_SPÄRRAD),
    (LOGIN, 2, BEHÖRIGHET_MARKERAD),
    (LOGIN, 2, BEHÖRIGHET_SAKNAS),
    (LOGIN, 1, r"\blogga?\s+in\b"),      # ensam: under tröskeln, skickas till modellen
    # Obehörig åtkomst till andras uppgifter: informationssäkerhet och åtkomstkontroll
    # (Patientdatalagen 4 kap.), inte journalföring -> ovrigt med tvungen granskning.
    (OTHER, 3, ÅTKOMST_LOGG),
    (OTHER, 3, UTAN_BEHÖRIGHET),        # "någon har läst min journal utan behörighet"
    (OTHER, 2, OBEHÖRIG_ÅTKOMST),
    (OTHER, 2, ANNAN_SER),
    (OTHER, 2, LOGGUPPFÖLJNING),
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

# Samma mönster som ovan, men nu som krav på en människa. De träffar etiketter som
# annars kan beslutas automatiskt: felaktig_inloggning (lösenordsåterställning löser
# dem inte) och ovrigt (ett åtkomstärende får inte tystas bort). Bedömningen görs på
# den normaliserade texten oavsett undantagsmönstret - ett undantag ska aldrig dölja
# ett behov av en människa.
REVIEW_MÖNSTER = (SAMTIDIG, DUBBEL, STULEN_SESSION, NEKAD, BEHÖRIGHET_SPÄRRAD,
                  BEHÖRIGHET_MARKERAD, BEHÖRIGHET_SAKNAS) + ÅTKOMSTMÖNSTER

# Mönster som kräver FLERA signaler samtidigt. Ett verktygsnamn eller en
# autentiseringsmetod ensam får aldrig avgöra en etikett: SITHS, BankID och HSA
# väger därför bara 1 poäng och bara i inloggningssammanhang (logga in, inloggning,
# kort, legitimation). Utan kontext träffar de inte alls, och tillsammans kan de
# aldrig väga över en tydlig journalträff eller ett behörighetsärende.
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


def _träffar(mönster: tuple, haystack: str) -> bool:
    """Sant om minst ett av mönstren matchar den normaliserade texten."""
    return any(re.search(m, haystack, re.IGNORECASE) for m in mönster)


def decide_rules(text: str) -> dict:
    """Poäng per kategori. En entydig etikett får confidence, annars label=None.

    kräver_granskning och routing_hint är oberoende av etiketten: vissa
    inloggningsärenden (delad session, åtkomst nekad trots rätt behörighet) ska till en
    människa även när reglerna är entydiga, och åtkomstärenden (obehörig läsning,
    åtkomstlogg, logguppföljning) ska till behörighetsansvarig oavsett vilken etikett
    poängen pekar på - se REVIEW_MÖNSTER och ROUTING_HINT.
    """
    haystack = normalize(text)
    scores: dict[str, float] = {}
    hits: list[dict] = []
    undantag = bool(UNDANTAG.search(haystack))
    granska = _träffar(REVIEW_MÖNSTER, haystack)
    hint = ROUTING_HINT if _träffar(ÅTKOMSTMÖNSTER, haystack) else None
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
                "margin": 0.0, "scores": {}, "hits": [], "undantag": undantag,
                "kräver_granskning": granska, "routing_hint": hint}
    topp, top_score = ranked[0]
    margin = top_score - (ranked[1][1] if len(ranked) > 1 else 0.0)
    # Lopp mellan kategorier ger ingen etikett: toppen är då godtyckligt vald, och
    # det är precis den etiketten en granskare eller modellforslag skulle jämföras mot.
    confident = top_score >= MIN_SCORE and margin >= MIN_MARGIN
    return {"label": topp if confident else None, "topp": topp, "confident": confident,
            "margin": round(margin, 2),
            "confidence": round(min(0.95, 0.6 + 0.15 * top_score), 2) if confident else 0.0,
            "scores": scores, "hits": hits, "undantag": undantag,
            "kräver_granskning": granska, "routing_hint": hint}


def beskriv_scores(rr: dict) -> str:
    """Regelpoängen som mänskligt läsbar text: svenska etiketter, fallande ordning."""
    ordnad = sorted(rr["scores"].items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join("{} {}".format(display_label(etikett), poang) for etikett, poang in ordnad) \
        or "inga träffar"