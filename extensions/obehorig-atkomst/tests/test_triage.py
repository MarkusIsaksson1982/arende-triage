"""Regler, integritet, schema, modellsteg, motor, granskning, audit, mätetal, CLI.

Allt deterministiskt och offline. Testerna skriver bara till tempfile - aldrig till
data/ - så sviten går att köra i en skrivskyddad arbetskatalog.
"""
import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from triage import matning, privacy, rules, schema
from triage.cli import main
from triage.model import load_fixtures, safe_call, stub_model, text_key
from triage.pipeline import TriageEngine, apply_approvals, audit_record, read_jsonl, review_queue

DATA = Path(__file__).resolve().parent.parent / "data"
README = Path(__file__).resolve().parent.parent / "README.md"
EXAMPLES = read_jsonl(DATA / "examples.jsonl")
HOLDOUT = read_jsonl(DATA / "holdout.jsonl")
APPROVALS = json.loads((DATA / "approvals.json").read_text(encoding="utf-8"))
MODELSVAR = {"label": "ovrigt", "confidence": 0.9, "rationale": "ok"}
TRASIGA = [dict(MODELSVAR, chain_of_thought="..."), dict(MODELSVAR, label="patient"),
           dict(MODELSVAR, confidence=2), dict(MODELSVAR, confidence=float("nan")),
           dict(MODELSVAR, confidence=float("inf")),
           {k: v for k, v in MODELSVAR.items() if k != "rationale"}]
PNR = "19870312-4514"
# Referensdatum för århundradet i tioåriga personnummer (injiceras, så att testerna
# inte flyttar med kalendern).
IDAG = date(2026, 10, 15)
AUDITFÄLT = ("reviewer", "reviewed_by", "reviewed_at", "review_note", "note_masked",
             "previous_label", "new_label", "timestamp")


def triage(text, case_id="sop", **kwargs):
    return TriageEngine(**kwargs).triage(text, case_id)


def alla_beslut():
    return [triage(c["text"], c["case_id"]) for c in EXAMPLES]


def tyst(fn, *argv):
    """Kör en CLI-kommando utan att spamma testutdata."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*argv)


class TestRegler(unittest.TestCase):
    def test_tydliga_fall(self):
        for text, etikett in [("Jag kan inte logga in, det står ogiltigt användarnamn.", rules.LOGIN),
                              ("Dokumenterat felaktig dosering i journalen.", rules.JOURNAL),
                              ("Sessionen går ut när jag sparar.", rules.LOGIN)]:
            with self.subTest(text=text):
                beslut = rules.decide_rules(text)
                self.assertEqual((beslut["label"], beslut["confident"]), (etikett, True))

    def test_osakert_när_toppen_eller_marginalen_svag(self):
        for text in ["Det går inte att logga in och någon har rapporterat en avvikelse.",
                     "Det finns en avvikelse som ska utredas.", "Kan inte logga in"]:
            with self.subTest(text=text):
                self.assertFalse(rules.decide_rules(text)["confident"])

    def test_journalsystemet_är_ett_verktyg_inte_journalföring(self):
        r = rules.decide_rules("Jag kan inte logga in i journalsystemet, det står ogiltigt användarnamn.")
        self.assertEqual((r["label"], r["confident"]), (rules.LOGIN, True))
        self.assertNotIn(rules.JOURNAL, r["scores"], "journalsystemet får ingen journalpoäng")

    def test_sammansatt_journalord_träffar(self):
        for text in ["Patientjournalen saknas helt.", "Journalföringen brister.",
                     "Journalanteckningen saknar signering.", "Vårdtillfället saknas i journalen."]:
            with self.subTest(text=text):
                self.assertEqual(rules.decide_rules(text)["topp"], rules.JOURNAL)

    def test_spärrad_och_blev_utloggad(self):
        r = rules.decide_rules("Personalen blev utloggad direkt när hen öppnade en spärrad lista.")
        self.assertEqual((r["label"], r["confident"]), (rules.LOGIN, True))
        self.assertIn("blev utloggad direkt", [h["phrase"] for h in r["hits"]])
        self.assertEqual(rules.decide_rules("Behörigheten är spärrad för alla.")["topp"], rules.ACCESS)

    def test_undantagsmönster_håller_när_det_bör(self):
        r = rules.decide_rules("Patienten ringer och vill ha en ny inloggning.")
        self.assertTrue(r["undantag"])
        self.assertEqual(r["hits"], [])
        self.assertFalse(rules.decide_rules("Samtalsunderlaget saknas.")["undantag"])

    def test_undantagsmönster_släpper_igenom_dokumenterat_fel(self):
        r = rules.decide_rules("Patienten samtalar och vi hittade dokumenterat felaktig dos.")
        self.assertTrue(r["undantag"])
        self.assertEqual(r["label"], rules.JOURNAL)
        self.assertGreaterEqual(r["scores"][rules.JOURNAL], 3)

    def test_undantagsmönster_ser_genom_maskat_namn(self):
        r = rules.decide_rules("Patienten [namn-maskat] ringer och vill ha en ny inloggning.")
        self.assertTrue(r["undantag"], "maskningen får inte dölja undantaget")
        self.assertEqual(r["hits"], [])

    def test_fel_patient_och_förväxling_har_egna_mönster(self):
        for text in ["Journalen visar fel person i vårdtillfället.",
                     "Förväxling av patient på avdelningen.",
                     "Sammanblandning av två journaler."]:
            with self.subTest(text=text):
                r = rules.decide_rules(text)
                self.assertEqual(r["topp"], rules.JOURNAL)
                self.assertIn(rules.JOURNAL, rules.ALWAYS_REVIEW)

    def test_samtidig_inloggning_är_åtkomstfråga(self):
        r = rules.decide_rules("Två personer var inloggade samtidigt på samma patient.")
        self.assertEqual(r["label"], rules.ACCESS)
        self.assertIn(rules.ACCESS, rules.ALWAYS_REVIEW)

    def test_lop_mellan_kategorier_görs_inte_till_etikett(self):
        r = rules.decide_rules("Kan inte logga in och det finns en avvikelse som ska utredas.")
        self.assertEqual(r["margin"], 0)
        self.assertIsNone(r["label"], "godtycklig topp får inte bli beslutets etikett")
        self.assertIsNotNone(r["topp"])

    def test_whitespace_handteras(self):
        self.assertEqual(rules.decide_rules("Sessionen går  ut"), rules.decide_rules("Sessionen går ut"))


class TestInloggningsmetoder(unittest.TestCase):
    """SITHS/BankID/HSA: ordgräns och inloggningskontext, aldrig en egen etikett."""

    def test_metod_namn_utan_kontext_träffar_inte(self):
        for text in ["Användaren har SITHS-kort.", "Kontot är kopplat till BankID.",
                     "Lösningen bygger på HSA."]:
            with self.subTest(text=text):
                r = rules.decide_rules(text)
                self.assertFalse(r["confident"])
                self.assertLessEqual(r["scores"].get(rules.LOGIN, 0), 1)

    def test_metod_namn_i_inloggningskontext_träffar(self):
        r = rules.decide_rules("Legitimering med BankID misslyckades, kan inte logga in.")
        self.assertEqual((r["label"], r["confident"]), (rules.LOGIN, True))
        fraser = [h["phrase"].lower() for h in r["hits"]]
        self.assertIn("bankid", fraser)
        kontext = [h for h in r["hits"] if h.get("rule")]
        self.assertEqual(kontext[0]["weight"], 1.0)
        self.assertEqual(kontext[0]["rule"], "SITHS/BankID/HSA i inloggningskontext")

    def test_metod_namn_kan_inte_väga_över_en_åtkomstfråga(self):
        text = "Åtkomstloggen granskades efter att SITHS-kortet användes."
        r = rules.decide_rules(text)
        self.assertEqual((r["label"], r["confident"]), (rules.ACCESS, True))
        self.assertEqual(r["scores"][rules.LOGIN], 1.0)

    def test_metod_namn_kan_inte_väga_över_en_journalavvikelse(self):
        text = "Dokumenterat felaktig dosering i journalen, trots HSA-kort och BankID."
        r = rules.decide_rules(text)
        self.assertEqual((r["label"], r["confident"]), (rules.JOURNAL, True))
        self.assertLess(r["scores"][rules.LOGIN], r["scores"][rules.JOURNAL])

    def test_ordgräns_hindrar_verktygsnamn(self):
        r = rules.decide_rules("Kan inte logga in i bankidsystemet.")
        self.assertLessEqual(r["scores"].get(rules.LOGIN, 0), 1,
                             "bankidsystemet är ett verktyg, inte en autentiseringsmetod")
        self.assertFalse(r["confident"])


class TestEtiketter(unittest.TestCase):
    def test_ascii_id_med_svensk_visning(self):
        self.assertEqual(sorted(schema.LABEL_DISPLAY), sorted(schema.LABELS))
        self.assertEqual(schema.display_label("obehorig_atkomst"), "Obehörig åtkomst")
        for etikett, visning in schema.LABEL_DISPLAY.items():
            with self.subTest(etikett=etikett):
                self.assertEqual(etikett, visning.lower().replace("å", "a").replace("ä", "a")
                                 .replace("ö", "o").replace(" ", "_"))
                self.assertIn(etikett, schema.LABELS)

    def test_varje_beslut_bär_båda(self):
        for beslut in alla_beslut():
            with self.subTest(case=beslut["case_id"]):
                self.assertIn(beslut["label"], schema.LABELS)
                self.assertEqual(beslut["label_display"], schema.display_label(beslut["label"]))

    def test_visningen_används_i_mänskligt_utdata(self):
        text = triage("Dokumenterat felaktig dosering i journalen.", "syn-visning")
        self.assertIn("Avvikelse i patientjournal", text["rationale"] + " ".join(text["trace"]))
        self.assertIn("Avvikelse i patientjournal", rules.beskriv_scores(
            rules.decide_rules("Dokumenterat felaktig dosering i journalen.")))


class TestIntegritet(unittest.TestCase):
    def test_personnummer_upptäcks(self):
        for text in ["patient " + PNR, "850312-4516", "198703124514", "Patient 19870312+4514"]:
            with self.subTest(text=text):
                self.assertEqual(privacy.find_personnummer(text), [text.split()[-1]])

    def test_ogiltiga_datum_maskeras_inte(self):
        for text in ["order 1234567890", "20240230-1234", "kostnad 45000 kronor"]:
            with self.subTest(text=text):
                self.assertEqual(privacy.find_personnummer(text), [])

    def test_mask_text_byter_ut_numret(self):
        maskad, kategorier = privacy.mask_text("Ring " + PNR + " imorgon.")
        self.assertEqual((maskad, kategorier),
                         ("Ring " + privacy.MASK + " imorgon.", ["personnummer"]))

    def test_namn_maskeras(self):
        for text, hemligt in [("Patient Anna Svensson kunde inte logga in.", "Anna Svensson"),
                              ("Journalen visar fel person, dr Lindqvist var inloggad.",
                               "Lindqvist"),
                              ("Granskat av dr Öberg tillsammans med Karin Ekström.", "Öberg")]:
            with self.subTest(text=text):
                maskad, kategorier = privacy.mask_text(text)
                self.assertIn("namn", kategorier)
                self.assertNotIn(hemligt, maskad)
                self.assertIn(privacy.NAMN_MASK, maskad)

    def test_syntetisk_namnlista_maskar_aven_utan_titel(self):
        maskad, kategorier = privacy.mask_text("Ärendet skapades av Karin Ekström.")
        self.assertIn("namn", kategorier)
        self.assertNotIn("Karin Ekström", maskad)
        self.assertNotIn("Karin Ekström", json.dumps(privacy.sanitize({"notat": "av Karin Ekström"}),
                                                    ensure_ascii=False))

    def test_titel_och_roll_får_stå_kvar(self):
        maskad, _ = privacy.mask_text("dr Lindqvist säger att åtkomst nekades.")
        self.assertTrue(maskad.startswith("dr "))
        self.assertIn(privacy.NAMN_MASK, maskad)

    def test_telefon_epost_och_identifierare_maskeras(self):
        for text, kategori, hemligt in [
                ("Ring 070-174 06 05 imorgon.", "telefon", "123 45 67"),
                ("Skicka svaret till anna.svensson@example.se.", "epost", "anna.svensson@example.se"),
                ("Patient-ID: AB12345 finns i loggen.", "identifierare", "AB12345")]:
            with self.subTest(text=text):
                maskad, kategorier = privacy.mask_text(text)
                self.assertIn(kategori, kategorier)
                self.assertNotIn(hemligt, maskad)
                self.assertIn(privacy.MASKER[kategori], maskad)

    def test_internt_ärendenummer_maskeras_inte(self):
        maskad, kategorier = privacy.mask_text("Ärendet heter syn-012 och patient 19870312-4514.")
        self.assertEqual(kategorier, ["personnummer"])
        self.assertIn("syn-012", maskad)

    def test_ogiltiga_ordningsnummer_blir_inte_personnummer(self):
        for text in ["order 1234567890", "kostnad 45000 kronor", "mobil 0701740605"]:
            with self.subTest(text=text):
                maskad, kategorier = privacy.mask_text(text)
                self.assertNotIn("personnummer", kategorier)
                self.assertTrue(kategorier == [] or "telefon" in kategorier)

    def text_med_allt(self):
        return ("Patient Anna Svensson, " + PNR + ", ringde 070-174 06 05 och skrev till "
                "anna.svensson@example.se om patient-ID: AB12345.")

    def test_modellen_ser_aldrig_identifierare(self):
        sedda = []
        beslut = TriageEngine(model_fn=lambda t: sedda.append(t) or None).triage(
            "Kan inte logga in, felaktig session för " + PNR + ".", "syn-pnr")
        self.assertTrue(sedda, "modellen ska ha anropats")
        self.assertNotIn("4514", sedda[0])
        self.assertIn(privacy.MASK, sedda[0])
        self.assertNotIn("4514", json.dumps(beslut, ensure_ascii=False))
        self.assertIn("personnummer maskade", beslut["trace"][0])

    def test_modellen_ser_inga_namn_telefoner_eller_epost(self):
        sedda = []
        rå = self.text_med_allt()
        beslut = TriageEngine(model_fn=lambda t: sedda.append(t) or None).triage(
            rå, "syn-allt")
        self.assertTrue(sedda, "modellen ska ha anropats")
        for hemligt in ("Anna", "Svensson", "070-123", "anna.svensson@example.se", "AB12345"):
            with self.subTest(hemligt=hemligt):
                self.assertNotIn(hemligt, sedda[0])
        ut = json.dumps(beslut, ensure_ascii=False)
        for hemligt in ("Anna", "Svensson", "070-123", "anna.svensson@example.se", "AB12345"):
            with self.subTest(utdata=hemligt):
                self.assertNotIn(hemligt, ut)
        self.assertEqual(sorted(beslut["masked_identifiers"]),
                         ["epost", "identifierare", "namn", "personnummer", "telefon"])

    def test_rå_text_förekommer_aldrig_i_beslutet(self):
        rå = "Kan inte logga in med 19870312-4514 eftersom kontot är låst."
        ut = json.dumps(triage(rå, "syn-002"), ensure_ascii=False)
        self.assertNotIn(rå, ut)
        self.assertNotIn("4514", ut)

    def test_notat_maskeras(self):
        notat = "Kontakta Anna Svensson på 070-174 06 05 angående patient 19870312-4514."
        maskat = privacy.mask_note(notat)
        for hemligt in ("Anna Svensson", "070-174 06 05", "4514"):
            with self.subTest(hemligt=hemligt):
                self.assertNotIn(hemligt, maskat)
        self.assertIn(privacy.NAMN_MASK, maskat)

    def test_ovarlig_text_maskeras_inte(self):
        for fall in EXAMPLES + HOLDOUT:
            if "19870312" in fall["text"]:     # hol-13 innehåller medvetat ett personnummer
                continue
            with self.subTest(text=fall["text"][:28]):
                self.assertEqual(privacy.mask_text(fall["text"]), (fall["text"], []))

    def test_sanitize_maskerar_djupt(self):
        gömda = {"a": ["text " + PNR, {"b": PNR}], "c": PNR, "d": 3}
        rengat = privacy.sanitize(gömda)
        self.assertNotIn("4514", json.dumps(rengat, ensure_ascii=False))
        self.assertEqual(rengat["d"], 3)


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.giltigt = triage(EXAMPLES[0]["text"], "syn-001")
        self.assertEqual(schema.validate_decision(self.giltigt), [])

    def test_avvisar_terk_beslut(self):
        trasiga = [dict(self.giltigt, label="patient"), dict(self.giltigt, hemligt_falt=1),
                   dict(self.giltigt, confidence=1.4), dict(self.giltigt, confidence="hog"),
                   dict(self.giltigt, trace=[]), dict(self.giltigt, needs_human_review="ja"),
                   dict(self.giltigt, previous_label="patient"),
                   dict(self.giltigt, case_id="ärende 1"),
                   dict(self.giltigt, label_display=""),
                   {k: v for k, v in self.giltigt.items() if k != "rationale"}]
        for beslut in trasiga:
            with self.subTest(beslut=beslut["label"]):
                self.assertTrue(schema.validate_decision(beslut))

    def test_beslut_får_inga_granskarfält(self):
        for fält in AUDITFÄLT:
            with self.subTest(fält=fält):
                self.assertTrue(schema.validate_decision(dict(self.giltigt, **{fält: "x"})))

    def test_ogiltigt_case_id_avvisas(self):
        for case_id in ["ärende 1", "", "a" * 65, "syn/001"]:
            with self.subTest(case_id=case_id):
                self.assertTrue(schema.validate(case_id, schema.CASE_ID))
                with self.assertRaises(ValueError):
                    triage("Kan inte logga in", case_id)
        self.assertEqual(schema.validate("syn-001", schema.CASE_ID), [])

    def test_icke_finita_tal_avvisas(self):
        for värde in [float("nan"), float("inf"), float("-inf")]:
            with self.subTest(värde=värde):
                self.assertTrue(schema.validate(värde, {"type": "number"}))
                self.assertTrue(schema.validate_decision(dict(self.giltigt, confidence=värde)))
                self.assertTrue(schema.validate_model_output(dict(MODELSVAR, confidence=värde)))

    def test_giltigt_beslut_är_standardiserad_json(self):
        for beslut in alla_beslut():
            with self.subTest(case=beslut["case_id"]):
                json.dumps(beslut, ensure_ascii=False, allow_nan=False)

    def test_bool_ar_inte_tal_och_modellsvar_valideras_strikt(self):
        self.assertTrue(schema.validate(True, {"type": "number"}))
        self.assertEqual(schema.validate(0.5, {"type": "number"}), [])
        self.assertEqual(schema.validate_model_output(MODELSVAR), [])
        for trasig in TRASIGA:
            with self.subTest(nyckel=list(trasig)[1]):
                self.assertTrue(schema.validate_model_output(trasig))

    def test_auditregistret_har_eget_schema_och_egen_version(self):
        self.assertNotEqual(schema.AUDIT_SCHEMA_VERSION, schema.SCHEMA_VERSION)
        giltig = {"schema_version": schema.AUDIT_SCHEMA_VERSION, "case_id": "syn-003",
                  "reviewer": "granskare-2", "timestamp": "2026-02-03T09:05:00Z",
                  "previous_label": "obehorig_atkomst", "new_label": "avvikelse_i_patientjournal",
                  "note_masked": "byte etikett"}
        self.assertEqual(schema.validate_audit(giltig), [])
        trasiga = [dict(giltig, schema_version="9.9.9"), dict(giltig, case_id="ärende 1"),
                   dict(giltig, timestamp="igår"), dict(giltig, timestamp="2026-02-03"),
                   dict(giltig, reviewer=""), dict(giltig, new_label="patient"),
                   dict(giltig, extra=1)]
        for post in trasiga:
            with self.subTest(post=sorted(post)[0]):
                self.assertTrue(schema.validate_audit(post))
        for fält in schema.AUDIT_SCHEMA["required"]:
            with self.subTest(saknas=fält):
                self.assertTrue(schema.validate_audit(
                    {k: v for k, v in giltig.items() if k != fält}))


class TestModellsteg(unittest.TestCase):
    def test_stub_ar_deterministisk_och_avstår(self):
        case = next(c for c in EXAMPLES if c["case_id"] == "syn-010")
        self.assertEqual(stub_model(case["text"]), stub_model(case["text"]))
        self.assertEqual(stub_model(case["text"])["label"], "avvikelse_i_patientjournal")
        self.assertIsNone(stub_model("helt ny text " + "x" * 20))

    def test_nyckel_beror_inte_pa_case_id(self):
        fall, omdöpt = EXAMPLES[8], dict(EXAMPLES[8], case_id="annat-id")
        self.assertEqual(text_key(fall["text"]), text_key(omdöpt["text"]))

    def test_trasiga_svar_blir_abstain_eller_invalid_schema(self):
        giltig = '{"label": "ovrigt", "confidence": 0.9, "rationale": "ok"}'

        def krasch(text):
            raise RuntimeError("modellen nere")
        for fn, status in [(lambda t: giltig, "ok"), (lambda t: '{"label": "ovri', "invalid_schema"),
                           (lambda t: dict(MODELSVAR, confidence=float("nan")), "invalid_schema"),
                           (lambda t: None, "abstain"), (krasch, "error")]:
            with self.subTest(status=status):
                self.assertEqual(safe_call(fn, "x")[1], status)

    def test_fixtures_följer_reglerna(self):
        """Varje fixture ska gälla ett fall som faktiskt når modellsteget."""
        for mängd in (EXAMPLES, HOLDOUT):
            modellfall = [c for c in mängd if not rules.decide_rules(c["text"])["confident"]]
            svar = [c for c in modellfall if stub_model(c["text"])]
            self.assertTrue(svar, "stubben ska svara minst en gång")
            for fall in modellfall:
                with self.subTest(case=fall["case_id"]):
                    self.assertIn(triage(fall["text"], fall["case_id"])["source"],
                                  ("modell_forslag", "abstain"))
            regelfall = {text_key(c["text"]) for c in mängd
                         if rules.decide_rules(c["text"])["confident"]}
            self.assertEqual(set(load_fixtures()) & regelfall, set(),
                             "fixture för ett fall som aldrig når modellen")


class TestMotor(unittest.TestCase):
    def test_alla_exempel_motsvarar_förväntning(self):
        for case in EXAMPLES:
            with self.subTest(case=case["case_id"]):
                beslut = triage(case["text"], case["case_id"])
                self.assertEqual((beslut["label"], beslut["needs_human_review"]),
                                 (case["expected_label"], case["expected_review"]))
                self.assertEqual(schema.validate_decision(beslut), [])

    def test_abstain_är_ingen_sannolikhet_för_ovrigt(self):
        def svag(text):
            return {"label": "avvikelse_i_patientjournal", "confidence": 0.2, "rationale": "gissning"}
        beslut = triage("Batteriet i hjärtstartaren byttes.", model_fn=svag)
        self.assertEqual((beslut["label"], beslut["source"], beslut["needs_human_review"]),
                         (rules.OTHER, "abstain", True))
        self.assertEqual(beslut["confidence"], 0.0,
                         "konfidensen avser det avvisade förslaget, inte etiketten övrigt")
        self.assertEqual((beslut["proposed_label"], beslut["proposed_confidence"],
                          beslut["model_status"]), ("avvikelse_i_patientjournal", 0.2, "ok"))

    def test_abstain_från_trasigt_svar_har_ingen_proposed(self):
        beslut = triage("Batteriet i hjärtstartaren byttes.", model_fn=lambda t: {"label": "ovrigt"})
        self.assertEqual((beslut["confidence"], beslut["source"], beslut["model_status"]),
                         (0.0, "abstain", "invalid_schema"))
        self.assertNotIn("proposed_label", beslut)

    def test_journal_och_åtkomst_granskas_alla(self):
        for text in ["Dokumenterat felaktig dosering i journalen.", "Journalen visar fel person.",
                     "Åtkomst nekad trots rätt behörighet.",
                     "Två personer var inloggade samtidigt på samma patient."]:
            with self.subTest(text=text):
                beslut = triage(text, "syn-x")
                self.assertEqual(beslut["source"], "rules", "reglerna räcker för etiketten")
                self.assertTrue(beslut["needs_human_review"])
                self.assertIn("alltid", beslut["trace"][-1])

    def test_fel_patient_granskas_även_om_modellen_avstår(self):
        beslut = triage("Journalen visar fel person.", "syn-018", model_fn=lambda t: None)
        self.assertEqual((beslut["label"], beslut["needs_human_review"]), (rules.JOURNAL, True))

    def test_sop_in_ga_är_inte_krasch(self):
        for text in ["", "   ", "?!?", "x" * 5000, "LOGGA IN LOGGA IN", "😀 inget nytt"]:
            with self.subTest(text=text[:16]):
                self.assertEqual(schema.validate_decision(triage(text)), [])

    def test_modellen_anropas_bara_för_osäkra_fall(self):
        kall = []
        motor = TriageEngine(model_fn=lambda t: kall.append(t) or None)
        motor.triage("Kan inte logga in", "a")
        motor.triage("Dokumenterat felaktig dosering i journalen.", "b")
        self.assertEqual(len(kall), 1, "bara osäkra fall ska göra modellanrop")

    def test_motsägelse_mellan_regler_och_modell_markeras(self):
        def felaktig(text):
            return {"label": "felaktig_inloggning", "confidence": 0.9, "rationale": "annan tolkning"}
        def rätt(text):
            return {"label": "avvikelse_i_patientjournal", "confidence": 0.9, "rationale": "samma spår"}
        text = "Anteckningen saknar signering och det går inte att logga in."
        beslut = triage(text, model_fn=felaktig)
        self.assertTrue(beslut["disagreement"], "reglerna pekade på journal, modellen på inloggning")
        self.assertEqual((beslut["label"], beslut["needs_human_review"]),
                         ("felaktig_inloggning", True))
        self.assertEqual(beslut["rule_top_label"], rules.JOURNAL)
        self.assertFalse(triage(text, model_fn=rätt)["disagreement"])

    def test_tröskeln_valideras(self):
        for värde in [1.5, -0.1, float("nan"), float("inf")]:
            with self.subTest(tröskel=värde):
                with self.assertRaises(ValueError):
                    TriageEngine(model_min_confidence=värde)


class TestGranskning(unittest.TestCase):
    def test_kön_innehåller_bara_överstående(self):
        ko = review_queue(alla_beslut())
        self.assertTrue(ko)
        for beslut in ko:
            with self.subTest(case=beslut["case_id"]):
                self.assertIn(beslut["source"], ("modell_forslag", "abstain", "rules"))
                self.assertEqual(schema.validate_decision(beslut), [])

    def test_ogiltigt_notat_ignoreras(self):
        beslut = alla_beslut()
        trasiga = [{"syn-013": {"label": "godkand", "reviewer": "", "reviewed_at": "idag"}},
                   {"syn-013": {"label": "felaktig_inloggning", "reviewer": "granskare-1"}},
                   {"syn-013": {"label": "felaktig_inloggning", "reviewer": "granskare-1",
                                "reviewed_at": "på tisdag"}},
                   {"syn-999": {"label": "ovrigt", "reviewer": "granskare-1",
                                "reviewed_at": "2026-02-03T09:00:00Z"}}]
        for notat in trasiga:
            with self.subTest(notat=sorted(notat)):
                self.assertEqual(apply_approvals(beslut, notat), (beslut, []))

    def test_alla_notat_gäller_kända_fall(self):
        kända = {c["case_id"] for c in EXAMPLES}
        for case_id, notat in APPROVALS.items():
            with self.subTest(case=case_id):
                self.assertEqual(schema.validate_approval(notat), [])
                self.assertIn(case_id, kända)

    def test_varje_fall_i_granskningskön_får_ett_notat(self):
        oförsta = [b["case_id"] for b in review_queue(alla_beslut()) if b["case_id"] not in APPROVALS]
        self.assertEqual(oförsta, [], "dessa fall skulle stanna kvar i granskningskön")
        beslut, _ = apply_approvals(alla_beslut(), APPROVALS)
        self.assertEqual(review_queue(beslut), [])


class TestAudit(unittest.TestCase):
    def granskade(self):
        return apply_approvals(alla_beslut(), APPROVALS)

    def test_notat_skriver_över_beslutet(self):
        beslut, _ = self.granskade()
        granskad = next(d for d in beslut if d["source"] == "manuell")
        self.assertEqual((granskad["confidence"], granskad["needs_human_review"]), (1.0, False))
        self.assertEqual(schema.validate_decision(granskad), [])
        self.assertIn("mänskligt beslut", granskad["trace"][-1])
        self.assertEqual(granskad["label_display"], schema.display_label(granskad["label"]))
        for före, nu in zip(alla_beslut(), beslut):
            if nu["source"] != "manuell":
                self.assertEqual(före, nu, "bara granskade fall ska förändras")

    def test_audit_ligger_separat_fran_besluten(self):
        beslut, audit = self.granskade()
        self.assertEqual(len(audit), len(APPROVALS))
        for rad in beslut:
            with self.subTest(case=rad["case_id"]):
                for fält in AUDITFÄLT:
                    self.assertNotIn(fält, rad, "granskaruppgifter får inte ligga i beslutet")

    def test_auditpostens_fält(self):
        _, audit = self.granskade()
        for post in audit:
            with self.subTest(case=post["case_id"]):
                self.assertEqual(sorted(post), sorted(schema.AUDIT_SCHEMA["required"]))
                self.assertEqual(schema.validate_audit(post), [])
                self.assertEqual(post["schema_version"], schema.AUDIT_SCHEMA_VERSION)
                self.assertIn(post["case_id"], {c["case_id"] for c in EXAMPLES})
                self.assertIn(post["previous_label"], schema.LABELS)
                self.assertIn(post["new_label"], schema.LABELS)
                self.assertTrue(post["reviewer"] and post["timestamp"])

    def test_audit_notaten_maskeras(self):
        notat = {"syn-013": {"label": "avvikelse_i_patientjournal", "reviewer": "granskare-2",
                             "reviewed_at": "2026-02-03T10:11:00Z",
                             "note": "Kontakta dr Lindqvist på 070-174 06 05 om 19870312-4514."}}
        _, audit = apply_approvals(alla_beslut(), notat)
        self.assertEqual(len(audit), 1)
        post = audit[0]
        for hemligt in ("Lindqvist", "070-174 06 05", "4514"):
            with self.subTest(hemligt=hemligt):
                self.assertNotIn(hemligt, post["note_masked"])
        self.assertIn(privacy.NAMN_MASK, post["note_masked"])

    def test_beslut_läcker_inte_granskarens_notat(self):
        """Notatets fritext får inte finnas i beslutet - bara en hänvisning till audit."""
        notat = {"syn-013": {"label": "avvikelse_i_patientjournal", "reviewer": "granskare-2",
                             "reviewed_at": "2026-02-03T10:11:00Z",
                             "note": "Kontakta dr Lindqvist på 070-174 06 42 om 19870312-4514, "
                                     "se markör xyz987."}}
        beslut, audit = apply_approvals(alla_beslut(), notat)
        self.assertEqual(len(audit), 1)
        granskade = [b for b in beslut if b["source"] == "manuell"]
        self.assertEqual(len(granskade), 1)
        beslutet = granskade[0]
        fritext = beslutet["rationale"] + " " + " ".join(beslutet["trace"])
        for hemligt in ("Kontakta", "Lindqvist", "070-174 06 42", "4514", "markör", "xyz987",
                        privacy.NAMN_MASK, privacy.TELEFON_MASK, privacy.MASK):
            with self.subTest(hemligt=hemligt):
                self.assertNotIn(hemligt, fritext)
        # Beslutet pekar ut auditposten med granskarens namn och tidpunkt, inte med text.
        self.assertIn(audit[0]["reviewer"], beslutet["rationale"])
        self.assertIn(audit[0]["timestamp"], beslutet["rationale"])
        self.assertIn("auditregistret", beslutet["rationale"])
        # Auditposten bär fortfarande den maskerade notatstexten.
        self.assertIn(privacy.NAMN_MASK, audit[0]["note_masked"])
        self.assertEqual(schema.validate_decision(beslutet), [])

    def test_ogiltigt_notat_gör_ingen_auditpost(self):
        for notat in ({"syn-013": {"label": "ovrigt", "reviewer": "g", "reviewed_at": "snart"}},
                      {"syn-013": {"label": "ovrigt", "reviewer": "g", "hemligt_fält": 1,
                                   "reviewed_at": "2026-02-03T10:11:00Z"}},
                      {"syn-404": {"label": "ovrigt", "reviewer": "g",
                                   "reviewed_at": "2026-02-03T10:11:00Z"}}):
            with self.subTest(notat=sorted(notat)):
                beslut, audit = apply_approvals(alla_beslut(), notat)
                self.assertEqual(audit, [])
                self.assertEqual(beslut, alla_beslut())

    def test_audit_record_fungerar_ensam(self):
        beslut = triage("Journalen visar fel person.", "syn-018")
        notat = dict(APPROVALS["syn-018"])
        post = audit_record(beslut, notat)
        self.assertEqual(schema.validate_audit(post), [])
        self.assertEqual(post["previous_label"], beslut["label"])
        self.assertEqual(post["new_label"], notat["label"])
        self.assertIsNone(audit_record(beslut, None))
        self.assertIsNone(audit_record(beslut, dict(notat, reviewed_at="snart")))


class TestMatning(unittest.TestCase):
    def setUp(self):
        self.m = matning.utvardera(HOLDOUT)

    def test_mätetal_på_syntetisk_holdout(self):
        self.assertEqual(self.m["antal"], len(HOLDOUT))
        self.assertGreaterEqual(self.m["precision"], 0.9)
        self.assertGreaterEqual(self.m["recall"], 0.6)
        self.assertGreaterEqual(self.m["träffsäkerhet"], 0.7)
        self.assertGreater(self.m["abstain_andel"], 0.1)
        self.assertGreater(self.m["granskningsandel"], 0.2)

    def test_abstain_och_fel_faller_i_granskningskön(self):
        for rad in self.m["rader"]:
            if rad["source"] == "abstain" or not rad["korrekt"]:
                with self.subTest(case=rad["case_id"]):
                    self.assertTrue(rad["review"], "inget fel eller avstående får tystas bort")
        self.assertTrue(self.m["fel"], "holdouten ska innehålla fall där kedjan avstår")

    def test_regel_modell_konflikter_räknas(self):
        konflikter = self.m["konflikter"]
        self.assertGreater(konflikter["antal"], 0, "holdouten ska innehålla en konflikt")
        self.assertEqual(konflikter["fall"], [r["case_id"] for r in self.m["rader"]
                                              if r["disagreement"]])
        self.assertEqual(sum(p["antal"] for p in konflikter["per_par"].values()),
                         konflikter["antal"])
        self.assertEqual(konflikter["andel"],
                         round(konflikter["antal"] / len(HOLDOUT), 3))
        self.assertEqual(konflikter["varav_fel"],
                         [r["case_id"] for r in self.m["rader"]
                          if r["disagreement"] and not r["korrekt"]])

    def test_tröskelsvep_visar_etapper_och_abstain(self):
        svep = self.m["svep"]
        self.assertEqual(svep["trösklar"], [0.5, 0.6, 0.7, 0.8, 0.9])
        for rad in svep["rader"]:
            with self.subTest(tröskel=rad["tröskel"]):
                self.assertEqual(sorted(rad["per_etikett"]), sorted(schema.LABELS))
                for m in rad["per_etikett"].values():
                    for nyckel in ("precision", "recall"):
                        self.assertGreaterEqual(m[nyckel], 0.0)
                        self.assertLessEqual(m[nyckel], 1.0)
                self.assertGreaterEqual(rad["abstain_andel"], 0.0)
                self.assertLessEqual(rad["abstain_andel"], 1.0)
        avstående = [r["abstain_andel"] for r in svep["rader"]]
        self.assertEqual(avstående, sorted(avstående),
                         "högre tröskel kan inte ge färre avståenden")
        self.assertLess(avstående[0], avstående[-1],
                        "svepet ska visa en verklig avvägning")

    def test_svep_beror_på_tröskeln(self):
        låg = matning.tröskelsvep(HOLDOUT, trösklar=(0.5,))
        hög = matning.tröskelsvep(HOLDOUT, trösklar=(0.9,))
        self.assertLess(låg["rader"][0]["abstain_andel"], hög["rader"][0]["abstain_andel"])
        self.assertGreater(låg["rader"][0]["recall"], hög["rader"][0]["recall"],
                       "en lägre tröskel fångar fler riktiga etiketter men avstår mindre")

    def test_rapporten_redovisar_etikett_konflikt_svep_och_juridik(self):
        text = matning.rapport(self.m)
        for etikett in schema.LABELS:
            self.assertIn(etikett, text)
        for visning in schema.LABEL_DISPLAY.values():
            self.assertIn(visning, text)
        for nyckel in ("precision", "recall", "abstain", "Regel/modell-konflikter",
                       "Tröskelsvep", "HSLF-FS 2017:40", "SOSFS 2011:9", "IVO",
                       "patientsäkerhetslagen (2010:659) 3 kap. 5 §",
                       "avgör inte rapporteringsplikt"):
            self.assertIn(nyckel, text)

    def test_holdout_är_läsbar(self):
        self.assertTrue(HOLDOUT)
        for fall in HOLDOUT:
            with self.subTest(case=fall["case_id"]):
                self.assertTrue(fall["text"].strip())
                self.assertIn(fall["expected_label"], schema.LABELS)


class TestCli(unittest.TestCase):
    def test_klass_ko_godkännande_schema_och_matning(self):
        with tempfile.TemporaryDirectory() as tmp:
            ut = str(Path(tmp) / "ut.jsonl")
            self.assertEqual(tyst(main, ["klass", "-o", ut, "-i", str(DATA / "examples.jsonl")]), 0)
            beslut = read_jsonl(ut)
            self.assertEqual(len(beslut), len(EXAMPLES))
            self.assertTrue(any(b["needs_human_review"] for b in beslut))
            self.assertEqual(tyst(main, ["ko", "-i", ut]), 0)
            self.assertEqual(tyst(main, ["klass", "-o", ut, "-g", str(DATA / "approvals.json")]), 0)
            self.assertFalse(any(b["needs_human_review"] for b in read_jsonl(ut)))
            for kommando in (["schema"], ["matning", "-i", str(DATA / "holdout.jsonl")]):
                with self.subTest(kommando=kommando):
                    self.assertEqual(tyst(main, kommando), 0)

    def test_audit_skrivs_till_egen_fil(self):
        with tempfile.TemporaryDirectory() as tmp:
            beslutsfil = Path(tmp) / "beslut.jsonl"
            auditfil = Path(tmp) / "audit.jsonl"
            self.assertEqual(tyst(main, ["klass", "-o", str(beslutsfil), "-g",
                                         str(DATA / "approvals.json"), "-a", str(auditfil)]), 0)
            beslut = read_jsonl(beslutsfil)
            audit = read_jsonl(auditfil)
            self.assertEqual(len(audit), len(APPROVALS))
            self.assertEqual({p["case_id"] for p in audit},
                             {d["case_id"] for d in beslut if d["source"] == "manuell"})
            for post in audit:
                self.assertEqual(schema.validate_audit(post), [])
            for rad in beslut:
                for fält in AUDITFÄLT:
                    self.assertNotIn(fält, rad)
            for post in audit:
                self.assertEqual(schema.validate_audit(post), [])

    def test_audit_filen_får_inte_blanda_granskarfält(self):
        with tempfile.TemporaryDirectory() as tmp:
            auditfil = Path(tmp) / "audit.jsonl"
            tyst(main, ["klass", "-o", str(Path(tmp) / "b.jsonl"), "-g",
                        str(DATA / "approvals.json"), "-a", str(auditfil)])
            audit = read_jsonl(auditfil)
            self.assertEqual(sorted(audit[0]), sorted(schema.AUDIT_SCHEMA["required"]))

    def test_audit_får_inte_skrivas_till_samma_fil_som_besluten(self):
        with tempfile.TemporaryDirectory() as tmp:
            samma = str(Path(tmp) / "samma.jsonl")
            argv = ["klass", "-o", samma, "-g", str(DATA / "approvals.json"), "-a", samma]
            self.assertEqual(tyst(main, argv), 1)
            self.assertFalse(Path(samma).exists())

    def test_ogiltigt_case_id_avbryter(self):
        with tempfile.TemporaryDirectory() as tmp:
            fil = Path(tmp) / "in.jsonl"
            fil.write_text(json.dumps({"case_id": "ärende 1",
                                       "text": "Kan inte logga in"}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                triage("Kan inte logga in", "ärende 1")
            self.assertEqual(tyst(main, ["klass", "-i", str(fil)]), 1)

    def test_ko_visar_svenska_etiketter(self):
        with tempfile.TemporaryDirectory() as tmp:
            ut = str(Path(tmp) / "ut.jsonl")
            tyst(main, ["klass", "-o", ut, "-i", str(DATA / "examples.jsonl")])
            ko = io.StringIO()
            with contextlib.redirect_stdout(ko), contextlib.redirect_stderr(io.StringIO()):
                main(["ko", "-i", ut])
            self.assertIn("Avvikelse i patientjournal", ko.getvalue())
            self.assertIn("avvikelse_i_patientjournal", ko.getvalue())
            self.assertIn("Obehörig åtkomst", ko.getvalue())

    def test_schema_visar_etikettöversättningen(self):
        ut = io.StringIO()
        with contextlib.redirect_stdout(ut), contextlib.redirect_stderr(io.StringIO()):
            main(["schema"])
        text = ut.getvalue()
        for etikett in schema.LABELS:
            self.assertIn(etikett, text)
        self.assertIn("Obehörig åtkomst", text)

    def test_okänt_kommando_och_ogiltiga_flaggor_avbryter(self):
        for argv in (["kvadd"], ["klass", "-t", "1,5"], ["klass", "-t", "abc"], ["klass", "-i"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as fel:
                    tyst(main, argv)
                self.assertEqual(fel.exception.code, 2)

    def test_inga_filer_skrivs_utan_ut(self):
        mal = DATA / "decisions.jsonl"
        före = mal.read_text(encoding="utf-8") if mal.exists() else None
        with tempfile.TemporaryDirectory() as tmp:
            gammal = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(tyst(main, ["klass", "-i", str(DATA / "examples.jsonl")]), 0)
                self.assertEqual(os.listdir(tmp), [], "inga filer ska skrivas utan -o")
            finally:
                os.chdir(gammal)
        self.assertEqual(före, mal.read_text(encoding="utf-8") if mal.exists() else None,
                         "data/decisions.jsonl ska inte röras")

    def test_korrupt_jsonl_och_dubblerad_case_id_avvisas(self):
        rader = {'{"case_id": "a", "text": "Kan inte logga in"}\n{ohi\n': "trasig",
                 '{"case_id": "a", "text": "Kan inte logga in"}\n'
                 '{"case_id": "a", "text": "Kunde inte logga in"}\n': "dubblerad",
                 '{"case_id": "a"}\n': "saknar_text",
                 '{"case_id": "a", "text": "Kan inte logga in"}\n': "saknar_forvantad_etikett"}
        with tempfile.TemporaryDirectory() as tmp:
            for innehåll, namn in rader.items():
                fil = Path(tmp) / (namn + ".jsonl")
                fil.write_text(innehåll, encoding="utf-8")
                kommando = ["matning"] if namn.endswith("etikett") else ["klass"]
                with self.subTest(fil=namn):
                    self.assertEqual(tyst(main, kommando + ["-i", str(fil)]), 1)

    def test_ko_kräver_indata(self):
        self.assertEqual(tyst(main, ["ko"]), 1)

    def test_modulstart_ger_exitkod_noll(self):
        def start():
            runpy.run_module("triage", run_name="__main__")
        argv = sys.argv
        sys.argv = ["triage", "schema"]
        try:
            with self.assertRaises(SystemExit) as fel:
                tyst(start)
            self.assertEqual(fel.exception.code, 0)
        finally:
            sys.argv = argv

    def test_stdout_innehåller_inga_identifierare(self):
        med_text = ("Patient Anna Svensson kan inte logga in för 19870312-4514 "
                    "(felaktig session), tel 070-174 06 05.")
        with tempfile.TemporaryDirectory() as tmp:
            indata = Path(tmp) / "in.jsonl"
            indata.write_text(json.dumps({"case_id": "pnr-1", "text": med_text}) + "\n",
                              encoding="utf-8")
            ut = io.StringIO()
            with contextlib.redirect_stdout(ut), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["klass", "-i", str(indata)]), 0)
            for hemligt in ("4514", "Anna", "Svensson", "070-123", med_text):
                with self.subTest(hemligt=hemligt):
                    self.assertNotIn(hemligt, ut.getvalue())



class TestRunda9(unittest.TestCase):
    """Runda 9 portad från rotversionen: Luhn/århundrade/samordningsnummer, telefon, säker hantering."""

    IDAG = date(2026, 1, 1)

    def setUp(self):
        self.m = matning.utvardera(HOLDOUT)

    def test_bindestreck_och_plus_ger_hundra_årsgränsen(self):
        """'-' = under 100 år, '+' = 100 år eller mer, båda med giltig Luhn."""
        self.assertEqual(privacy.find_personnummer("260115-0010", IDAG), ["260115-0010"],
                         "två siffror och '-' ger det aktulla århundradet (2026)")
        self.assertEqual(privacy.find_personnummer("260115+0010", IDAG), ["260115+0010"],
                         "samma siffror med '+' läses som 1926")
        self.assertEqual(privacy.find_personnummer("000229+0013", IDAG), [],
                         "med '+' blir 000229 1900-02-29, som inte finns")

    def test_datum_belopp_och_ordernummer_maskeras_inte_som_telefon(self):
        for text in ["2000-02-29", "4500,00 kr", "order 1234567890", "070174",
                     "12345678901", "patient 19870312-4514"]:
            with self.subTest(text=text):
                maskad, kategorier = privacy.mask_text(text)
                self.assertNotIn("telefon", kategorier,
                                 "ett tal som inte är svenskt numrformat ska inte maskas")
                if "personnummer" not in kategorier:
                    self.assertEqual(kategorier, [])
                    self.assertEqual(maskad, text)

    def test_foedda_pa_2000tal_och_skottdag(self):
        """2000 är ett skottår: 29 februari ska hittas, både i 10- och 12-sifferform."""
        for pnr in ["20000229-0013", "000229-0013", "240229-0015", "0002290013",
                    "990228-0008"]:
            with self.subTest(pnr=pnr):
                self.assertEqual(privacy.find_personnummer("pnr " + pnr, IDAG), [pnr])
        for pnr in ["20010229-0013", "19990229-0005","19000229-0013"]:
            with self.subTest(pnr=pnr):
                self.assertEqual(privacy.find_personnummer("pnr " + pnr, IDAG), [],
                                 "29 februari finns bara i skottår")

    def test_luhn_avvisar_fel_kontrollsiffra(self):
        """Samma datum, en kontrollsiffra fel: den ena hittas och den andra inte."""
        for pnr in ["20000229-0013", "000229-0013", "0002290013", "19260115-0010"]:
            with self.subTest(pnr=pnr):
                self.assertEqual(privacy.find_personnummer("pnr " + pnr, IDAG), [pnr])
        for pnr in ["20000229-0014", "000229-0012", "0002290012", "19260115-0011"]:
            with self.subTest(pnr=pnr):
                self.assertEqual(privacy.find_personnummer("pnr " + pnr, IDAG), [],
                                 "kontrollsiffran ska räcka för att maskas")

    def test_rad_utan_acceptabla_utfall_är_aldrig_säker(self):
        beslut = {"label": rules.JOURNAL, "source": "rules", "needs_human_review": True}
        for case in ({}, {"ambiguous": True}, {"ambiguous": True, "acceptable": []}):
            with self.subTest(case=case):
                self.assertFalse(matning._säker(beslut, case),
                                 "utan dokumenterade acceptable är inget utfall godkänt")
        self.assertTrue(matning._säker(beslut, {"acceptable": ["granskning"]}))
        self.assertTrue(matning._säker(dict(beslut, source="abstain"),
                                       {"acceptable": ["abstain"]}))
        self.assertFalse(matning._säker(dict(beslut, needs_human_review=False, source="rules"),
                                        {"acceptable": ["granskning", "abstain"]}))

    def test_rapporten_redovisar_säker_hantering_separat(self):
        text = matning.rapport(self.m)
        self.assertIn("säker hantering av tvetydiga fall: 3/3 (100%)", text)
        self.assertIn("precision 1.00 | recall (makro) 0.88 | träffsäkerhet 0.86", text)

    def test_samordningsnummer_adderar_sextio(self):
        """Samordningsnummer har dagen +60: 61-91 är den 1:a-31:a."""
        for pnr in ["850372-0016", "19850372-0016", "870391-0003"]:
            with self.subTest(pnr=pnr):
                self.assertEqual(privacy.find_personnummer("pnr " + pnr, IDAG), [pnr])
        self.assertEqual(privacy.find_personnummer("000292-0015", IDAG), [],
                         "dag 92 är inte ett samordningsnummer och inte ett datum")

    def test_svep_redovisar_säker_hantering_per_tröskel(self):
        for rad in self.m["svep"]["rader"]:
            with self.subTest(tröskel=rad["tröskel"]):
                self.assertEqual(rad["säker_hantering_andel"], 1.0)
                self.assertGreaterEqual(rad["säker_hantering_andel"], 0.0)
                self.assertLessEqual(rad["säker_hantering_andel"], 1.0)
        self.assertIn("säker", matning._svep_rader(self.m["svep"])[-1])

    def test_säker_hantering_följer_acceptable_och_inte_omvänd(self):
        """Ett tvetydigt fall som besvaras utan granskning är inte säkert hanterat."""
        fall = [{"case_id": "syn-tv", "text": "Kan inte logga in, felaktigt lösenord.",
                 "expected_label": rules.JOURNAL, "ambiguous": True,
                 "acceptable": [rules.JOURNAL, "abstain", "granskning"],
                 "motivering": "syntetiskt tvetydigt fall"}]
        m = matning.utvardera(fall)
        self.assertEqual(m["säker_hantering"],
                         {"antal": 1, "säkert": 0, "andel": 0.0, "osäkra": ["syn-tv"]},
                         "kedjan svarade utan granskning - det är inte säker hantering")
        self.assertIn("syn-tv", matning.rapport(m))

    def test_säker_hantering_är_ett_eget_mått(self):
        """Säker hantering mäter de tvetydiga fallen - och rörar inte de strikta måtten."""
        säkert = self.m["säker_hantering"]
        self.assertEqual((säkert["antal"], säkert["säkert"]), (3, 3))
        self.assertEqual(säkert["andel"], 1.0)
        self.assertEqual(säkert["osäkra"], [])
        for rad in self.m["rader"]:
            if rad["case_id"] in ("hol-14", "hol-15", "hol-19"):
                with self.subTest(case=rad["case_id"]):
                    self.assertTrue(rad["ambiguous"])
                    self.assertTrue(rad["säker"], "avstående eller granskning är säkert")
        self.assertEqual(self.m["fel"], ["hol-14", "hol-15", "hol-19"])
        self.assertEqual(self.m["träffsäkerhet"],
                         round(sum(1 for r in self.m["rader"] if r["korrekt"]) / len(HOLDOUT), 3))
        self.assertEqual(self.m["precision"], 1.0)
        self.assertEqual(self.m["recall"], 0.875)  # fyra etiketter: obehorig_atkomst 4/4 lyfter makro-recall

    def test_telefon_i_mobil_och_46_form(self):
        for nummer in ["070-174 06 05","0701740605","+46 70 174 06 05","+46701740605",
                       "070-174 06 99", "070 174 06 42"]:
            with self.subTest(nummer=nummer):
                maskad, kategorier = privacy.mask_text("Ring " + nummer + " imorgon.")
                self.assertEqual(kategorier, ["telefon"])
                self.assertEqual(maskad, "Ring " + privacy.TELEFON_MASK + " imorgon.")

    def test_tvetydiga_fall_är_markerade_med_motivering(self):
        """Varje tvetydig rad ska säga VARFÖR den är tvetydig och vad som är godkänt."""
        tvetydiga = {"hol-14", "hol-15", "hol-19"}
        for fall in HOLDOUT:
            with self.subTest(case=fall["case_id"]):
                if fall["case_id"] not in tvetydiga:
                    self.assertNotIn("ambiguous", fall,
                                     "en entydig rad ska inte märkas som tvetydig")
                    continue
                self.assertTrue(fall["ambiguous"])
                self.assertTrue(fall["acceptable"], "vilka utfall som är godkända ska stå i raden")
                self.assertTrue(fall["motivering"].strip(), "tvetydigheten ska motiveras")
                self.assertEqual(fall["expected_label"], rules.JOURNAL,
                                 "guldetiketten ändras inte - den mäts mot stigningen")
                self.assertIn("abstain", fall["acceptable"])
                self.assertIn("granskning", fall["acceptable"])

    def test_århundradet_läses_av_referensdatumet(self):
        """Samma tio siffror, olika århundrade beroende på referensdatumet.

        00 02 29 är 2000-02-29 (skottår) eller 1900-02-29 (inte skottår), så testet
        kan skilja århundradet utan att gissa.
        """
        self.assertEqual(privacy.find_personnummer("000229-0013", IDAG), ["000229-0013"])
        self.assertEqual(privacy.find_personnummer("000229-0013", date(1901, 3, 1)), [],
                         "1900 var inte skottår, så det går inte att hitta 29 februari där")


class TestReadmeRunda9(unittest.TestCase):
    """README ska beskriva den mätning som faktiskt körs (portat från roten, fyra etiketter)."""

    @classmethod
    def setUpClass(cls):
        cls.m = matning.utvardera(HOLDOUT)

    def test_readme_förklarar_granskningskön(self):
        readme = " ".join(README.read_text(encoding="utf-8").split())
        for bit in ("76", "100", "1,25–2,5 timmar", "tröskel", "recall", "granskningskön",
                    "informationssäkerhetssamordnare", "behörighetsansvarig",
                    "Patientdatalagen (2008:355) 4 kap.", "logguppföljning"):
            with self.subTest(bit=bit):
                self.assertIn(bit, readme)

    def test_readmes_mätetal_stämmer_med_körningen(self):
        readme = " ".join(README.read_text(encoding="utf-8").split())
        m = self.m
        self.assertIn(
            "precision {:.2f}, makro-recall {:.2f}, träffsäkerhet {:.2f}, abstain {:.0f} %".format(
                m["precision"], m["recall"], m["träffsäkerhet"], 100 * m["abstain_andel"]), readme)
        self.assertIn("granskningskö {:.0f} %, {} konflikt av {} fall".format(
            100 * m["granskningsandel"], m["konflikter"]["antal"], m["antal"]), readme)
        self.assertIn("{} av {} ({:.0f} %)".format(m["säker_hantering"]["säkert"],
                                                   m["säker_hantering"]["antal"],
                                                   100 * m["säker_hantering"]["andel"]), readme)
        låg, hög = m["svep"]["rader"][0], m["svep"]["rader"][-1]
        for rad in (låg, hög):
            self.assertIn("vid {} är recall {:.2f} och {:.0f} % avstår".format(
                rad["tröskel"], rad["recall"], 100 * rad["abstain_andel"]), readme)


if __name__ == "__main__":
    unittest.main()