"""Kommandoradsverktyg: python3 -m triage klass|ko|schema|matning [flaggor]

Argumenten valideras av argparse: okänt kommando, saknat flaggvärde och ogiltig
tröskel avbryter med felkod 2 istället för att tyst gissa. Utan -o skrivs inga
filer - besluten skrivs bara till stdout, så en typo aldrig kan skriva över
data/decisions.jsonl (genererad utdata ska inte ligga i repot).

Mänskliga beslut hamnar i en SEPARAT fil (-a/--audit): auditregistret har eget
schema och egen version och blandas aldrig ihop med besluten.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import matning, schema
from .model import stub_model
from .pipeline import (MODEL_MIN_CONFIDENCE, TriageEngine, apply_approvals, read_jsonl,
                       review_queue, write_jsonl)

DATA = Path(__file__).resolve().parent.parent / "data"
KOMMANDON = {"klass": "klassificera ärenden och skriv beslut till stdout",
             "ko": "visa återstående granskningskö från en beslutsfil",
             "schema": "skriv ut besluts-schemat",
             "matning": "precision/recall, konflikter, tröskelsvep och säker hantering av "
                        "tvetydiga fall på den syntetiska holdouten"}


def _tröskel(värde: str) -> float:
    try:
        siffra = float(str(värde).replace(",", "."))
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("tröskeln måste vara ett tal, fick {!r}".format(värde))
    if not 0.0 <= siffra <= 1.0:   # fångar även NaN och ±inf
        raise argparse.ArgumentTypeError("tröskeln måste ligga mellan 0 och 1, fick {}".format(
            siffra))
    return siffra


def _bygg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage", description="Ärendetriage: felaktig inloggning, journalavvikelse, "
                                   "övrigt.")
    sub = parser.add_subparsers(dest="kommando", required=True,
                                metavar="{" + ",".join(KOMMANDON) + "}")
    for namn, hjälp in KOMMANDON.items():
        p = sub.add_parser(namn, help=hjälp, description=hjälp)
        if namn == "schema":
            continue
        p.add_argument("-i", "--input", "--inmatning", dest="input", metavar="FIL",
                       help="indata i JSONL (se data/)")
        p.add_argument("-o", "--ut", "--out", dest="ut", metavar="FIL",
                       help="valfri utfil för beslut; utan -o skrivs inga filer")
        if namn == "klass":
            p.add_argument("-g", "--godkännande", "--godkannande", dest="godkannande",
                           metavar="FIL", help="mänskliga notat (JSON)")
            p.add_argument("-a", "--audit", dest="audit", metavar="FIL",
                           help="valfri utfil för auditregistret (skrivs bara med -g)")
            p.add_argument("-m", "--modell", "--model", dest="modell", choices=["stub", "ingen"],
                           default="stub", help="stub = deterministisk offline-modell")
            p.add_argument("-t", "--tröskel", "--threshold", dest="troskel", type=_tröskel,
                           default=MODEL_MIN_CONFIDENCE,
                           help="minsta modellkonfidens (0–1, default {})".format(
                               MODEL_MIN_CONFIDENCE))
    return parser


def _motor(flag) -> TriageEngine:
    fn = stub_model if flag.modell == "stub" else (lambda text: None)
    return TriageEngine(model_fn=fn, model_min_confidence=flag.troskel)


def _indata(sökväg):
    rader = read_jsonl(sökväg)
    sedda = set()
    for rad, fall in enumerate(rader, start=1):
        if not isinstance(fall.get("text"), str) or not fall["text"].strip():
            raise ValueError("rad {} i {} saknar ett 'text'-fält".format(rad, sökväg))
        case_id = fall.get("case_id") or "rad-{}".format(rad)
        if case_id in sedda:
            raise ValueError("duplicerad case_id i {}: {}".format(sökväg, case_id))
        if schema.validate(case_id, schema.CASE_ID):
            raise ValueError("ogiltigt case_id i rad {} av {}: {!r} (tillåtna tecken: A-Z, "
                             "0-9, _ . : -)".format(rad, sökväg, case_id))
        sedda.add(case_id)
        fall["case_id"] = case_id
    return rader


def main(argv: list[str] | None = None) -> int:
    flag = _bygg_parser().parse_args(argv)
    try:
        return _kör(flag)
    except (OSError, ValueError) as fel:
        print("fel: {}".format(fel), file=sys.stderr)
        return 1


def _kör(flag) -> int:
    if flag.kommando == "schema":
        print(json.dumps(schema.DECISION_SCHEMA, ensure_ascii=False, indent=2))
        print("\nEtiketter (ascii-id -> svensk visning):")
        for etikett in schema.LABELS:
            print("  {:<26} {}".format(etikett, schema.display_label(etikett)))
        print("\nAuditregistret för mänskliga beslut har eget schema: schema.AUDIT_SCHEMA")
        return 0
    if flag.kommando == "matning":
        sökväg = flag.input or str(DATA / "holdout.jsonl")
        fall = _indata(sökväg)
        for rad, mätfall in enumerate(fall, start=1):
            if mätfall.get("expected_label") not in schema.LABELS:
                raise ValueError("rad {} i {} saknar giltig 'expected_label'".format(rad, sökväg))
        print(matning.rapport(matning.utvardera(fall)))
        return 0
    if flag.kommando == "ko":
        if not flag.input:
            raise ValueError("granskningskön läses från -i/--input (inga genererade filer "
                             "ligger i repot)")
        ko = review_queue(read_jsonl(flag.input))
        for beslut in ko:
            print("[{}] {} / {} (källa: {}, konfidens: {}{})".format(
                beslut["case_id"], schema.display_label(beslut.get("label")),
                beslut.get("label"), beslut["source"], beslut["confidence"],
                ", förslag var {}".format(schema.display_label(beslut["proposed_label"]))
                if "proposed_label" in beslut else ""))
        print("{} fall i granskningskö".format(len(ko)), file=sys.stderr)
        return 0
    fall = _indata(flag.input or str(DATA / "examples.jsonl"))
    if flag.audit and flag.ut and flag.audit == flag.ut:
        raise ValueError("auditregistret måste skrivas till en egen fil, inte till beslutsfilen")
    beslut = [_motor(flag).triage(c["text"], c["case_id"]) for c in fall]
    audit = []
    if flag.godkannande:
        beslut, audit = apply_approvals(
            beslut, json.loads(Path(flag.godkannande).read_text(encoding="utf-8")))
    if flag.ut:
        write_jsonl(flag.ut, beslut)
    if flag.audit:
        write_jsonl(flag.audit, audit)
    print("".join(json.dumps(b, ensure_ascii=False) + "\n" for b in beslut), end="")
    print("{} fall -> {} | {} i granskningskö".format(
        len(beslut), flag.ut or "stdout (ingen fil skriven)", len(review_queue(beslut))),
        file=sys.stderr)
    if flag.godkannande or flag.audit:
        print("{} mänskliga beslut -> {} (audit, egen fil)".format(
            len(audit), flag.audit or "skrivs inte utan -a"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())