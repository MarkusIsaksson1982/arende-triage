"""Mätetal på en liten syntetisk holdout (data/holdout.jsonl).

precision/recall per etikett, regel/modell-konflikter, tröskelsvep, abstain- och
granskningsandel - lokalt, utan nätverk och utan träningsdata. Måtetalet gäller den
etikett kedjan föreslår (före mänsklig granskning); "övrigt" räknas inte som positiv
klass, för etiketten betyder "oklassat" och inte "inget hände". Holdouten är
handskriven och liten: ett rökprov mot regressioner, inte ett prestandatest.
"""
from __future__ import annotations

from . import rules, schema
from .pipeline import TriageEngine

TRÖSKLAR = (0.5, 0.6, 0.7, 0.8, 0.9)


def _andel(tal, nämnare) -> float:
    return round(tal / nämnare, 3) if nämnare else 0.0


def utvardera(fall: list[dict], motor: TriageEngine | None = None,
              trösklar: tuple = TRÖSKLAR) -> dict:
    motor = motor or TriageEngine()
    rader = _rader(fall, motor)
    per_etikett = _per_etikett(rader)
    n = len(rader) or 1
    klassade = _klassade(per_etikett)
    return {"antal": len(rader), "per_etikett": per_etikett, "rader": rader,
            "precision": _mikro_precision(per_etikett),
            "recall": _andel(sum(m["recall"] for m in klassade.values()), len(klassade)),
            "träffsäkerhet": _andel(sum(1 for r in rader if r["korrekt"]), n),
            "abstain_andel": _andel(sum(1 for r in rader if r["source"] == "abstain"), n),
            "granskningsandel": _andel(sum(1 for r in rader if r["review"]), n),
            "konflikter": _konflikter(rader),
            "svep": tröskelsvep(fall, trösklar=trösklar, modell=motor.model_fn),
            "fel": [r["case_id"] for r in rader if not r["korrekt"]]}


def _klassade(per_etikett: dict) -> dict:
    return {e: m for e, m in per_etikett.items() if e != rules.OTHER}


def _mikro_precision(per_etikett: dict) -> float:
    klassade = _klassade(per_etikett).values()
    tp = sum(m["tp"] for m in klassade)
    fp = sum(m["fp"] for m in klassade)
    return _andel(tp, tp + fp)


def _rader(fall: list[dict], motor: TriageEngine) -> list[dict]:
    rader = []
    for case in fall:
        beslut = motor.triage(case["text"], case.get("case_id", "unknown"))
        rader.append({"case_id": case.get("case_id", "unknown"), "expected": case["expected_label"],
                      "predicted": beslut["label"], "source": beslut["source"],
                      "review": beslut["needs_human_review"], "disagreement": beslut["disagreement"],
                      "regelns_topp": beslut.get("rule_top_label"),
                      "modellens_forslag": beslut.get("proposed_label"),
                      "korrekt": beslut["label"] == case["expected_label"]})
    return rader


def _per_etikett(rader: list[dict]) -> dict:
    per_etikett = {}
    for etikett in sorted({r["expected"] for r in rader} | {r["predicted"] for r in rader}):
        tp = sum(1 for r in rader if r["expected"] == etikett == r["predicted"])
        fp = sum(1 for r in rader if r["predicted"] == etikett and r["expected"] != etikett)
        fn = sum(1 for r in rader if r["expected"] == etikett and r["predicted"] != etikett)
        per_etikett[etikett] = {"tp": tp, "fp": fp, "fn": fn,
                                "precision": _andel(tp, tp + fp), "recall": _andel(tp, tp + fn)}
    return per_etikett


def _konflikter(rader: list[dict]) -> dict:
    """Antal fall där regelns topp och modellens förslag pekar åt olika håll.

    En konflikt är inte i sig ett fel - men den är ett mätetal: ju fler, desto tydligare
    visar beslutet att de två spåren säger olika saker, och desto viktigare blir
    granskningskön.
    """
    fall = [r for r in rader if r["disagreement"]]
    per_par: dict[str, dict] = {}
    for rad in fall:
        nyckel = "{} -> {}".format(schema.display_label(rad["regelns_topp"]),
                                   schema.display_label(rad["modellens_forslag"]))
        per_par.setdefault(nyckel, {"antal": 0, "fall": []})
        per_par[nyckel]["antal"] += 1
        per_par[nyckel]["fall"].append(rad["case_id"])
    return {"antal": len(fall), "totalt": len(rader),
            "andel": _andel(len(fall), len(rader) or 1),
            "per_par": per_par, "fall": [r["case_id"] for r in fall],
            "varav_fel": [r["case_id"] for r in fall if not r["korrekt"]]}


def tröskelsvep(fall: list[dict], trösklar: tuple = TRÖSKLAR, modell=None) -> dict:
    """Kör holdouten flera gånger med olika modelltröskel.

    Svepet visar kvalhandlingen: en lägre tröskel ger fler förslag men fler fel, en
    högre ger färre förslag och mer avstående. Ingen tröskel är "rätt" utan mätning på
    granskade fall.
    """
    rader = []
    for tröskel in trösklar:
        motor = TriageEngine(model_fn=modell, model_min_confidence=tröskel)
        mätning = _rader(fall, motor)
        per_etikett = _per_etikett(mätning)
        klassade = _klassade(per_etikett).values()
        rader.append({"tröskel": round(float(tröskel), 2), "per_etikett": per_etikett,
                      "precision": _mikro_precision(per_etikett),
                      "recall": _andel(sum(m["recall"] for m in klassade), len(klassade)),
                      "abstain_andel": _andel(sum(1 for r in mätning if r["source"] == "abstain"),
                                              len(mätning) or 1),
                      "granskningsandel": _andel(sum(1 for r in mätning if r["review"]),
                                                 len(mätning) or 1),
                      "konflikter": sum(1 for r in mätning if r["disagreement"])})
    return {"trösklar": [round(float(t), 2) for t in trösklar], "rader": rader}


def rapport(matning: dict) -> str:
    rader = ["Mätetal på syntetisk holdout: {} fall (python3 -m triage matning)".format(
        matning["antal"]),
             "precision {:.2f} | recall (makro) {:.2f} | träffsäkerhet {:.2f}".format(
                 matning["precision"], matning["recall"], matning["träffsäkerhet"]),
             "abstain {:.0%} | kvar i granskningskö {:.0%} | fel: {}".format(
                 matning["abstain_andel"], matning["granskningsandel"],
                 ", ".join(matning["fel"]) or "inga"),
             "", "{:<28} {:>3} {:>3} {:>3} {:>9} {:>6}".format("etikett", "tp", "fp", "fn",
                                                             "precision", "recall")]
    for etikett, m in sorted(matning["per_etikett"].items()):
        rader.append("{:<28} {:>3} {:>3} {:>3} {:>9.2f} {:>6.2f}".format(
            etikett, m["tp"], m["fp"], m["fn"], m["precision"], m["recall"]))
    rader += _konflikt_rader(matning["konflikter"])
    rader += _svep_rader(matning["svep"])
    rader += ["", "Holdouten är syntetisk, handskriven och liten: ett rökprov, inte ett "
                  "prestandatest.",
              "Verktyget klassificerar text. Det avgör inte rapporteringsplikt (lex Maria: "
              "anmälan av vårdgivaren till IVO enligt patientsäkerhetslagen (2010:659) 3 kap. 5 §, "
              "HSLF-FS 2017:40),",
              "inte om en händelse är en patientsäkerhetshändelse och inte om en journal-"
              "avvikelse är en avvikelse enligt SOSFS 2011:9."]
    return "\n".join(rader)


def _konflikt_rader(konflikter: dict) -> list[str]:
    rader = ["", "Regel/modell-konflikter: {} av {} fall ({:.0%}) där regelns topp skilde sig "
              "från modellens förslag".format(konflikter["antal"], konflikter["totalt"],
                                              konflikter["andel"])]
    if not konflikter["fall"]:
        rader.append("  inga: reglerna och modellen pekade samma håll varje gång")
    for par, m in sorted(konflikter["per_par"].items()):
        rader.append("  {} fall: {} ({})".format(m["antal"], par, ", ".join(m["fall"])))
    rader.append("  av dessa fel i kedjan: {}".format(", ".join(konflikter["varav_fel"]) or "inga"))
    return rader


def _svep_rader(svep: dict) -> list[str]:
    rader = ["", "Tröskelsvep (modellens konfidensgräns): precision/recall per etikett och "
              "avstående andel"]
    for etikett in schema.LABELS:
        rader.append("  {}".format(schema.display_label(etikett)))
        delar = []
        for rad in svep["rader"]:
            m = rad["per_etikett"].get(etikett, {"precision": 0.0, "recall": 0.0})
            delar.append("{:.1f}: p {:.2f} r {:.2f} avstående {:.2f}".format(
                rad["tröskel"], m["precision"], m["recall"], rad["abstain_andel"]))
        rader.append("    " + " | ".join(delar))
    totals = " | ".join("{:.1f}: p {:.2f} r {:.2f} avst {:.2f} konfl {:.0f}".format(
        rad["tröskel"], rad["precision"], rad["recall"], rad["abstain_andel"], rad["konflikter"])
        for rad in svep["rader"])
    rader.append("  totalt: " + totals)
    return rader