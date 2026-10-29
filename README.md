# arende-triage

**Use when** an agent needs to triage Swedish-language support tickets (ärendetriage) with rules before an LLM, and to detect and mask Swedish personnummer and organisationsnummer (PII redaction) before any text reaches the model.

**English summary.** A small, offline Python demo that triages Swedish support-ticket text for a health-IT setting into
three labels – `felaktig_inloggning` (login failure), `avvikelse_i_patientjournal` (patient-record deviation) and
`ovrigt` (other) – and returns a self-defined JSON schema. Deterministic rules decide first; a pluggable language-model
step may only *propose* a label when the rules are unsure; uncertain cases, all journal deviations and all access
cases go to a human. Personal identifiers (personnummer with Luhn/century/samordningsnummer checks, organisationsnummer with
Luhn check, names, phone numbers, e-mail) are masked before rules, before the model step and in all output. Human decisions are kept in a
separate audit record. Standard library only, no network; the model step is a deterministic offline stub, so the
114 tests never call a model.

- **Demo only, synthetic data only.** Everything in `data/` is invented. Never run real patient tickets through it.
- **Not legal advice.** The legal references below are context for the design, not a compliance assessment.
  **Humans – not the tool – decide whether anything is a deviation or must be reported** (lex Maria / IVO etc.).
- **Optional extension:** [`extensions/obehorig-atkomst/`](extensions/obehorig-atkomst/) adds a fourth label for
  unauthorised access / log follow-up – see its README.
- **How it was built:** with AI-assisted workflows (several models drafting, cross-reviewing and patching), with
  every change reviewed and accepted by a human.

```bash
python3 -m unittest discover -s tests   # 114 tests, offline
python3 -m triage matning               # metrics on the synthetic holdout
```

Licence: MIT (see `LICENSE`). The rest of this README is in Swedish.

---

# Ärendetriage – inloggning, journalavvikelse, övrigt

Ett litet, offline-verktyg som läser en ärendetext och **föreslår** en etikett: `felaktig_inloggning`,
`avvikelse_i_patientjournal` eller `ovrigt`. Poängen är determinism, spårbarhet och en
skarp gräns mellan regler, språkmodell och människa.

> **Endast syntetisk data** – allt i `data/` är självskrivet och påhittat; kör aldrig verkliga patientärenden.
> **Verktyget avgör inte om något ska rapporteras** (se Rättslig ram nedan), och regelmönstren är inte validerade
> mot svensk vårdverksamhet.

## Kör det

```bash
python3 -m unittest discover -s tests          # 114 tester, offline, skriver bara till tempfile
python3 -m triage klass                        # klassificera data/examples.jsonl -> stdout
python3 -m triage ko -i /tmp/beslut.jsonl      # granskningskön, med svenska etiketter
python3 -m triage matning                      # precision/recall, konflikter, tröskelsvep
python3 -m triage schema                       # besluts-schemat + etikettöversättningen
python3 -m triage klass -o /tmp/slut.jsonl -g data/approvals.json -a /tmp/audit.jsonl
```

Flaggor: `-i/--input`, `-o/--ut` (beslut), `-g/--godkännande` (mänskliga notat), `-a/--audit` (auditfil),
`-m/--modell` (`stub` | `ingen`), `-t/--tröskel` (0–1, default 0.70). Okänt kommando, saknat värde och ogiltig
tröskel avbryter med felkod 2; utan `-o`/`-a` skrivs **inga** filer. Python 3.9+, bara standardbiblioteket.
API: `from triage import triage_case`.

## Schema

Etiketterna har **ascii-id och svensk visning**: `felaktig_inloggning` → *Felaktig inloggning*,
`avvikelse_i_patientjournal` → *Avvikelse i patientjournal*, `ovrigt` → *Övrigt*. Id:t används
i JSON, fixtures och nycklar, visningen i granskningskön, mätetal och annan mänsklig läsning. Beslut (schema 1.3.0, validerat av `triage/schema.py`):

```json
{"schema_version": "1.3.0", "case_id": "syn-012", "label": "felaktig_inloggning", "label_display": "Felaktig inloggning",
 "confidence": 0.78, "source": "modell_forslag", "needs_human_review": true, "model_status": "ok",
 "proposed_label": "felaktig_inloggning", "proposed_confidence": 0.78, "rule_top_label": "felaktig_inloggning",
 "disagreement": false, "masked_identifiers": ["personnummer"], "rationale": "Modellförslag, inväntar granskning: …",
 "rule_hits": [{"label": "felaktig_inloggning", "phrase": "Åtkomst nekad", "weight": 2.0}], "trace": ["…"]}
```

**Auditregistret för mänskliga beslut ligger separat från besluten** – egen fil, eget schema, egen version
(`schema.AUDIT_SCHEMA`). Granskarens fritext skrivs aldrig i ett beslut:

```json
{"schema_version": "1.0.0", "case_id": "syn-013", "reviewer": "granskare-2", "timestamp": "2026-02-03T10:11:00Z",
 "previous_label": "ovrigt", "new_label": "avvikelse_i_patientjournal", "note_masked": "Skrev över abstain: dokumentationsbristen väger tyngre."}
```

`case_id` och `timestamp` valideras mot mönster (inte bara "icke tomt"), och ett notat som saknar granskare,
tidpunkt eller giltig etikett ignoreras hellre än att krascha.

## Var modellen slutar och människan börjar

```
text ─► 0. maskera identifierare ─► 1. deterministiska regler ─► entydig? ─ ja ─► beslut
                                        │                        nej (svag topp/liten marginal/tyst)
                                        ▼                              │
                              2. modellens FÖRSLAG ─► 3. mänsklig granskning ─► 4. beslut
```

1. **Reglerna bestämmer när de är entydiga.** Etiketten vinner bara om poängen är ≥ 2 *och* marginalen mot
   nästa kategori är ≥ 2; vid lopp vinner ingen. `journalsystemet` är ett verktygsnamn och ger ingen journalpoäng.
   SITHS/BankID/HSA väger bara 1 poäng och bara i inloggningskontext (`logga in`, `inloggning`, `kort`,
   `legitimation`), med ordgräns på metodnamnet – `bankidsystemet` är inte en autentiseringsmetod – så att
   metoden aldrig kan väga över en tydlig journalträff eller ett behörighetsärende.
2. **Modellen får bara höra den svaga texten – och får bara föreslå.** Den anropas aldrig för ett fall reglerna
   redan avgjort. Gränssnittet är `model_fn(text) -> dict | None`; den ser bara texten, aldrig `case_id` eller
   regelpoängen, och svaret måste validera mot `MODEL_OUTPUT_SCHEMA`.
3. **Osäkra fall går till människa, alltid.** `needs_human_review` gäller varje modellförslag, varje avstående,
   och **journalavvikelse – samt de inloggningfall och åtkomstfall som är behörighets-, sessions- eller
   loggärenden – även när reglerna var entydiga**. Fel patient och förväxling har egna mönster och egna krav. Ett
   mänskligt notat skriver över beslutet sist i kedjan.

**Osäkra fall.** Ett svar som validerar och når tröskeln används som *förslag* och kräver granskning. Svar
under tröskeln, trasig JSON, extra fält, ogiltig etikett, NaN, kastat fel eller ingen fixture ger `ovrigt` med
`source=abstain`, konfidens **0.00** och `model_status` (`ok` | `invalid_schema` | `abstain` | `error`) – det
avvisade förslaget ligger i `proposed_*` och går vidare till granskning. `ovrigt` betyder alltså inte "inget
hände" utan "processen kan inte hantera fallet automatiskt", och granskningskön sorteras på `needs_human_review`,
aldrig på konfidenssiffran.

### Vart åtkomstärenden går i den här varianten

Uppgiften bad om tre etiketter, så åtkomst och behörighet måste hanteras av de tre som finns. Utgångspunkten är
inte vilken etikett som "stämmer bäst", utan vad som faktiskt ska hända med ärendet:

| Ärende | Etikett | Varför |
| --- | --- | --- |
| Någon har läst patientens journal **utan behörighet**, åtkomstlogg, obehörig åtkomst, annan användare kunde se uppgifter, logguppföljning | `ovrigt` + `routing_hint` | Informationssäkerhet och åtkomstkontroll enligt **Patientdatalagen (2008:355) 4 kap.**, inte journalföring. Egen etikett saknas, men ärendet får aldrig besvaras automatiskt: det pekas till behörighetsansvarig. |
| Delad eller stulen session, samtidig inloggning på samma patient | `felaktig_inloggning` | Det är en inloggning, men lösenordsbyte löser den inte. |
| Åtkomst nekad trots rätt behörighet, behörighet saknas eller är spärrad | `felaktig_inloggning` | "Jag kommer inte in" trots fungerande inloggning – ett behörighetsärende. |

De två sista raderna får etiketten `felaktig_inloggning`, men **alltid `needs_human_review: true`**: de styrs av
`rules.REVIEW_MÖNSTER` och ska till behörighetsansvarig, inte besvaras av support. Fellinjen – att låta dem
besvaras automatiskt som vanliga inloggningsfel – är det som varianten med fyra etiketter skulle undvika; därför
finns granskningsregeln kvar även när etiketten är densamma som för ett vanligt inloggningsfel.

Första raden fungerar på samma sätt: etiketten `ovrigt` är ett *beslut* om att ärendet inte hör till de tre
etiketterna, inte ett tystat bort-fall. `rules.ÅTKOMSTMÖNSTER` (samma strängar som vikterna i `PATTERNS`)
sätter både `needs_human_review: true` och `routing_hint: "behörighetsansvarig"`, och hänvisningen följer med
även när reglerna är så osäkra att ärendet hamnar i modellsteget. `schema.ROUTING_HINTS` är det enda tillåtna
värdet, så ett beslut inte kan peka någon annanstans. Övriga `ovrigt`-fall – som teknik och frågor – kan
fortfarande beslutas automatiskt. I ett blandat fall (dokumentationsfel och åtkomst i samma text) kan
journalpoängen vinna etiketten, men hänvisningen till behörighetsansvarig står kvar i beslutet, så att
åtkomstdelen följer med till granskningen.

## Mätetal (`python3 -m triage matning`)

21 syntetiska holdoutfall: precision, recall per etikett, träffsäkerhet, abstain- och granskningsandel,
**regel/modell-konflikter** (antal och per par) och ett **tröskelsvep** (0.5/0.6/0.7/0.8/0.9) med precision,
recall och avstående andel per etikett. Nu: precision 1.00, makro-recall 0.81, träffsäkerhet 0.86, abstain 29 %,
granskningskö 81 %, 1 konflikt av 21 fall. Svepet visar avvägningen: vid 0.5 är recall 0.88 och 24 % avstår, vid 0.9
är recall 0.68 och 38 % avstår – färre fel, mer till människa. `ovrigt` räknas inte som positiv klass, så de tre
åtkomstfallen (hol-05, hol-06, hol-20) lämnar makro-recallen när de klassas rätt; träffsäkerheten och precision
är oförändrade. Holdouten är liten och handskriven, alltså ett rökprov och inte ett prestandatest.

**Säker hantering** är ett eget mätetal, inte en del av de strikta talen. Tre fall (hol-14, hol-15, hol-19) är
medvetat tvetydiga: de bär `ambiguous`, `acceptable` och `motivering` i `data/holdout.jsonl`. Ett tvetydigt fall
är säkert hanterat när kedjan avstår eller lägger fallet i granskningskön – aldrig när hon gissar en etikett och
låter den passera. Nu: **3 av 3 (100 %)**. De strikta måtten ovan står kvar oförändrade, och de tre fallen är
fortfarande miss där: att flytta dem till "rätt" hade höjt siffrorna utan att säga något om kedjan.

### Vad granskningskön på 81 % betyder i praktiken

81 % är en arbetsmängd, inte en siffra: tar man 100 ärenden en dag går 81 till en människa. På 1–2 minuter per
ärende blir det ungefär 1,5–3 timmars granskningstid om dagen – en halvtid som går att lägga i schemat, en
flaskhals om den hamnar ovanpå befintliga skift. Kön växer med tröskeln, inte bara med reglerna: svepet ovan visar
att en lägre tröskel ger högre recall (0.88 vid 0.5) och mindre avstående, och en högre ger lägre recall (0.68 vid
0.9) och mer avstående. Eftersom även ett avstående hamnar i granskningskön byter tröskeln alltså recall mot mer
kö, inte mot färre ärenden hos människan – tröskeln ska väljas mot den tid verksamheten faktiskt har, inte mot en
siffra i en rapport. Åtkomst- och behörighetsärenden ligger alltid i kön oavsett tröskel och pekas till
informationssäkerhetssamordnare/behörighetsansvarig enligt **Patientdatalagen (2008:355) 4 kap.**, med
logguppföljning: det är den rollen som avgör behörighet och följer upp åtkomstloggen.

## Integritet

`mask_text()` körs före reglerna, före `model_fn` och på allt som skrivs ut. Den hittar och ersätter
`personnummer` (`19870312-4514`, `198703124514` – 10/12 siffror; giltigt datum, rätt århundrade, Luhn och
samordningsnummer med dagen +60), `organisationsnummer` (`559901-1235`, `168024007778` – tredje siffran
minst 2 och Luhn), `namn` (syntetisk namnlista samt titel/roll: "dr Lindqvist", "Patient Anna
Svensson"), `telefon` (`070-174 06 05`, `+46 70 174 06 05`), `epost` (`anna.svensson@example.se`) och andra
identifierare (`Patient-ID: AB12345`). Århundradet i tioåriget form läses mot ett referensdatum som skickas in
utifrån (`idag`), så att både kod och tester kan utgå från samma datum.

Överlappande träffar hanteras med prioritet (e-post → telefon → personnummer → organisationsnummer → identifierare → namn), så att
ett tioställigt telefonnummer inte rapporteras som personnummer. Telefonmönstret kräver ett svenskt
områdesnummer och en grupperad abonnentdel, så att datum (`2000-02-29`), belopp (`4500,00`) och
ordningsnummer (`1234567890`) inte maskas av en träff. Granskningsnotat maskeras innan de skrivs till
auditregistret, och `masked_identifiers` visar *vilka kategorier* som maskades – aldrig värdena. Detta är ett
**hinder, inte en dataskyddslösning**: orden fångar inte diagnoskoder eller all fritext, och kan övermaskera ett
vanligt ord. Verklig drift kräver dessutom gallring, åtkomstlogg, kryptering och incidenthantering.

## Rättslig ram – och var gränsen går

Ärendetexten är sjukvårdsdata enligt **Patientdatalagen (2008:355)**, vars 4 kap. reglerar behörighet,
åtkomst och logguppföljning. I verklig drift gäller även **SOSFS 2011:9** om ledningssystem för systematiskt
kvalitetsarbete, som omfattar avvikelsehanteringen, och **HSLF-FS 2017:40**, Socialstyrelsens föreskrifter
om anmälan av händelser som har medfört eller hade kunnat medföra allvarlig vårdskada (lex Maria). Anmälan
görs av vårdgivaren till **IVO** enligt **patientsäkerhetslagen (2010:659) 3 kap. 5 §**. Verktyget bedömer
inget av det och skapar ingen rapport eller utredning. **Människan – inte verktyget – avgör om ett ärende
är en avvikelse, en patientsäkerhetshändelse eller något som ska rapporteras**; etiketten är ett underlag i
den bedömningen.

## Variant: fyra etiketter

Det här är den strikt treetikettiga varianten som uppgiften bad om. Under arbetet med ärendeflötet visade det sig
dessutom finnas ett eget ärendeslag för **obehörig åtkomst och logguppföljning** – behörighet som saknas eller är
spärrad, åtkomst som nekas trots rätt behörighet, åtkomstloggar – som i vård-IT har egen utredning och eget
beslutsstöd och därför inte passar som `felaktig_inloggning`: felaktig klassning skickar frågan till support i
stället för till behörighetsansvarig. En fyraetikettig variant med den etiketten finns som tillägg i `extensions/obehorig-atkomst/`, och skillnaden mot den här
är etiketten, mönstren, routing_hint och de alltid-granskade fallen – inte kedjan, integritetslagret eller
auditregistret, som är oförändrade. Här hanteras de fallen i stället enligt tabellen ovan: otillåten åtkomst som
`ovrigt` med tvungen granskning och hänvisning till behörighetsansvarig, delad session och åtkomst nekad som
inloggning med obligatorisk granskning.

## Testfall för identifierare

`data/identifier_fixtures.json` innehåller syntetiska fall (personnummer, samordningsnummer, organisationsnummer,
fel kontrollsiffra, telefon) med förväntad maskad text och kategorier; `tests/test_identifiers.py` kör dem.
Siffrorna är påhittade och Luhn-giltiga där de ska vara, men är inte kontrollerade mot Bolagsverket.

## Test queries

Five queries that should find this repository in a code search such as [Xerj](https://xerj.dev), with the file
expected in the top 5 results (checked locally with `xerj code`, v1.0.0-rc.100, 2026-10-29):

| Query | Expected file |
|---|---|
| `validate Swedish personnummer Luhn samordningsnummer` | `triage/privacy.py` |
| `detect organisationsnummer Swedish organisation number` | `triage/privacy.py` |
| `mask PII before sending text to the LLM` | `triage/privacy.py` |
| `Swedish support ticket triage rules before model` | `triage/rules.py` |
| `human review queue audit record for triage decisions` | `triage/pipeline.py` |

## Filer

`triage/`: `schema.py`, `privacy.py`, `rules.py`, `model.py`, `pipeline.py`, `matning.py`, `cli.py`. `data/`:
`examples.jsonl` (18 ärenden), `holdout.jsonl` (21 mätfall), `fixtures.json` (modellsvar på hash av texten),
`approvals.json` (syntetiska notat). `tests/test_triage.py` och `tests/test_identifiers.py`: 114 tester; ingen modell anropas i testerna, och de
skriver bara till `tempfile`, aldrig till `data/`.