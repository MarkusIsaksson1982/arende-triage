# Extension: `obehorig_atkomst` (fourth label)

**The workflow identified another feature to add.** The original brief asked for exactly three labels, and the main
code at the repository root does exactly that. While working through the ticket flow, a fourth kind of ticket kept
showing up that fits none of the three: **unauthorised access and log follow-up** (missing or blocked permissions,
access denied despite the right role, access logs, simultaneous sessions on the same patient). In Swedish health IT
that is its own case type under Patientdatalagen (2008:355) 4 kap. – it belongs to the access owner
(behörighetsansvarig / informationssäkerhetssamordnare), not to password support. This extension gives it its own
label, `obehorig_atkomst`, always routed to human review, so it becomes visible in metrics and in the review queue.

**Status.** This is a self-contained copy of the package with four labels (86 tests:
`cd extensions/obehorig-atkomst && python3 -m unittest discover -s tests`). It is based on the iteration *before*
the latest fixes in the root version, so it does **not** yet include: Luhn/century/samordningsnummer validation of
personnummer, the tightened phone pattern, the separate "safe handling of ambiguous cases" metric and the
review-queue cost section. Port those from the root before using it as more than a comparison.

**Apply:** run this package instead of the root one (`cd extensions/obehorig-atkomst && python3 -m triage klass`),
or port the label into the root code: add `ACCESS = "obehorig_atkomst"` and its pattern block in `triage/rules.py`
(and to `ALWAYS_REVIEW`), add it to `LABELS`/`LABEL_DISPLAY` in `triage/schema.py`, and move the access cases in
`data/` from `ovrigt` + `routing_hint` to the new label. **Remove:** the reverse, about 10–15 minutes – see
"Hur lätt är den att ta bort?" below. It is a policy choice, not a code problem.

---

# Ärendetriage – inloggning, åtkomst, journalavvikelse, övrigt

Ett litet, offline-verktyg som läser en ärendetext och **föreslår** en etikett: `felaktig_inloggning`,
`obehorig_atkomst`, `avvikelse_i_patientjournal` eller `ovrigt`. Poängen är determinism, spårbarhet och en
skarp gräns mellan regler, språkmodell och människa.

> **Endast syntetisk data** – allt i `data/` är självskrivet och påhittat; kör aldrig verkliga patientärenden.
> **Verktyget avgör inte om något ska rapporteras** (se Rättslig ram nedan), och regelmönstren är inte validerade
> mot svensk vårdverksamhet.

## Kör det

```bash
python3 -m unittest discover -s tests          # 86 tester, offline, skriver bara till tempfile
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

Etiketterna har **ascii-id och svensk visning**: `felaktig_inloggning` → *Felaktig inloggning*, `obehorig_atkomst`
→ *Obehörig åtkomst*, `avvikelse_i_patientjournal` → *Avvikelse i patientjournal*, `ovrigt` → *Övrigt*. Id:t används
i JSON, fixtures och nycklar, visningen i granskningskön, mätetal och annan mänsklig läsning. Beslut (schema 1.2.0, validerat av `triage/schema.py`):

```json
{"schema_version": "1.2.0", "case_id": "syn-012", "label": "obehorig_atkomst", "label_display": "Obehörig åtkomst",
 "confidence": 0.78, "source": "modell_forslag", "needs_human_review": true, "model_status": "ok",
 "proposed_label": "obehorig_atkomst", "proposed_confidence": 0.78, "rule_top_label": "obehorig_atkomst",
 "disagreement": false, "masked_identifiers": ["personnummer"], "rationale": "Modellförslag, inväntar granskning: …",
 "rule_hits": [{"label": "obehorig_atkomst", "phrase": "Åtkomst nekad", "weight": 2.0}], "trace": ["…"]}
```

**Auditregistret för mänskliga beslut ligger separat från besluten** – egen fil, eget schema, egen version
(`schema.AUDIT_SCHEMA`). Granskarens fritext skrivs aldrig i ett beslut:

```json
{"schema_version": "1.0.0", "case_id": "syn-003", "reviewer": "granskare-2", "timestamp": "2026-02-03T09:05:00Z",
 "previous_label": "obehorig_atkomst", "new_label": "obehorig_atkomst", "note_masked": "Samtidig inloggning: logguppföljning …"}
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
   metoden aldrig kan väga över en tydlig journal- eller åtkomstträff.
2. **Modellen får bara höra den svaga texten – och får bara föreslå.** Den anropas aldrig för ett fall reglerna
   redan avgjort. Gränssnittet är `model_fn(text) -> dict | None`; den ser bara texten, aldrig `case_id` eller
   regelpoängen, och svaret måste validera mot `MODEL_OUTPUT_SCHEMA`.
3. **Osäkra fall går till människa, alltid.** `needs_human_review` gäller varje modellförslag, varje avstående,
   och **journalavvikelse och åtkomstfråga även när reglerna var entydiga**. Fel patient och förväxling har egna
   mönster och egna krav. Ett mänskligt notat skriver över beslutet sist i kedjan.

**Osäkra fall.** Ett svar som validerar och når tröskeln används som *förslag* och kräver granskning. Svar
under tröskeln, trasig JSON, extra fält, ogiltig etikett, NaN, kastat fel eller ingen fixture ger `ovrigt` med
`source=abstain`, konfidens **0.00** och `model_status` (`ok` | `invalid_schema` | `abstain` | `error`) – det
avvisade förslaget ligger i `proposed_*` och går vidare till granskning. `ovrigt` betyder alltså inte "inget
hände" utan "processen kan inte hantera fallet automatiskt", och granskningskön sorteras på `needs_human_review`,
aldrig på konfidenssiffran.

## Mätetal (`python3 -m triage matning`)

21 syntetiska holdoutfall: precision, recall per etikett, träffsäkerhet, abstain- och granskningsandel,
**regel/modell-konflikter** (antal och per par) och ett **tröskelsvep** (0.5/0.6/0.7/0.8/0.9) med precision,
recall och avstående andel per etikett. Nu: precision 1.00, makro-recall 0.88, träffsäkerhet 0.86, abstain 29 %,
1 konflikt av 21 fall. Svepet visar avvägningen: vid 0.5 är recall 0.92 och 24 % avstår, vid 0.9 är recall 0.78
och 38 % avstår – färre fel, mer till människa. `ovrigt` räknas inte som positiv klass; holdouten är liten och
handskriven, alltså ett rökprov och inte ett prestandatest.

## Integritet

`mask_text()` körs före reglerna, före `model_fn` och på allt som skrivs ut. Den hittar och ersätter
`personnummer` (`19870312-4519`, `198703124519` – 10/12 siffror, giltigt datum), `namn` (syntetisk namnlista
samt titel/roll: "dr Lindqvist", "Patient Anna Svensson"), `telefon` (`070-174 06 05`, `+46 70 174 06 05`),
`epost` (`anna.svensson@example.se`) och andra identifierare (`Patient-ID: AB12345`).

Överlappande träffar hanteras med prioritet (e-post → telefon → personnummer → identifierare → namn), så att
ett tioställigt telefonnummer inte rapporteras som personnummer. Granskningsnotat maskeras innan de skrivs till
auditregistret, och `masked_identifiers` visar *vilka kategorier* som maskades – aldrig värdena. Detta är ett
**hinder, inte en dataskyddslösning**: orden fångar inte samordningsnummer, diagnoskoder eller all fritext, och
kan övermaskera ett vanligt ord. Verklig drift kräver dessutom gallring, åtkomstlogg, kryptering och
incidenthantering.

## Rättslig ram – och var gränsen går

Ärendetexten är sjukvårdsdata enligt **Patientdatalagen (2008:355)**, vars 4 kap. reglerar behörighet,
åtkomst och logguppföljning. I verklig drift gäller även **SOSFS 2011:9** om ledningssystem för systematiskt
kvalitetsarbete, som omfattar avvikelsehanteringen, och **HSLF-FS 2017:40**, Socialstyrelsens föreskrifter
om anmälan av händelser som har medfört eller hade kunnat medföra allvarlig vårdskada (lex Maria). Anmälan
görs av vårdgivaren till **IVO** enligt **patientsäkerhetslagen (2010:659) 3 kap. 5 §**. Verktyget bedömer
inget av det och skapar ingen rapport eller utredning. **Människan – inte verktyget – avgör om ett ärende
är en avvikelse, en patientsäkerhetshändelse eller något som ska rapporteras**; etiketten är ett underlag i
den bedömningen.

## Extra funktion: `obehorig_atkomst`

Uppgiften bad om **tre** etiketter. Arbetet med ärendeflötet identifierade en fjärde som inte passar in i de tre:
**obehörig åtkomst och logguppföljning** – behörighet som saknas eller är spärrad, åtkomst som nekas trots rätt
behörighet, åtkomstloggar, samtidig inloggning på samma patient. I vård-IT är det ett eget ärendeslag med egen
utredning och eget beslutsstöd, inte "fel lösenord": felaktig klassning som `felaktig_inloggning` skickar
åtkomstfrågan till support i stället för till behörighetsansvarig. Därför finns den med, alltid granskningskrävande.

**Hur lätt är den att ta bort?** Cirka **10–15 minuter**, och borttagningen är helt symbolstyrt:

1. `triage/rules.py`: ta bort konstanten `ACCESS` och dess plats i `ALWAYS_REVIEW`, och radera mönsterblocket med
   åtkomstloggar och samtidig inloggning.
2. `triage/schema.py`: ta bort `ACCESS` ur `LABELS` och ur `LABEL_DISPLAY`.
3. `data/`: filtrera bort allt märkt `obehorig_atkomst` i `examples.jsonl`, `holdout.jsonl`, `approvals.json` och
   `fixtures.json`.
4. `tests/test_triage.py`: ta bort testerna vars namn innehåller *åtkomst*.
5. Kör testerna.

Den strikt treetikettiga huvudversionen finns i repots rot: där får åtkomstärendena etiketten
`felaktig_inloggning` eller `ovrigt` med obligatorisk granskning och hänvisning till behörighetsansvarig. Det är en
policyfråga, inte en kodfråga: etiketten gör logguppföljningen synlig i mätetal och granskningskö, vilket en
sjunken etikett inte gör.

## Filer

`triage/`: `schema.py`, `privacy.py`, `rules.py`, `model.py`, `pipeline.py`, `matning.py`, `cli.py`. `data/`:
`examples.jsonl` (18 ärenden), `holdout.jsonl` (21 mätfall), `fixtures.json` (modellsvar på hash av texten),
`approvals.json` (syntetiska notat). `tests/test_triage.py`: 86 tester; ingen modell anropas i testerna, och de
skriver bara till `tempfile`, aldrig till `data/`.