"""Samordning: regler först, modell bara när reglerna är osäkra, alltid manuell
granskning innan något ändras. Stegen (detta är det som bedöms):
  1. Ärendetexten maskeras (personnummer, namn, telefon, e-post, andra identifierare)
     innan någon annan komponent får se den.
  2. Reglerna ger en entydig etikett -> beslut utan modellanrop.
  3. Reglerna är osäkra eller tysta -> modellsteget får ge ett FÖRSLAG, och varje
     förslag kräver mänsklig granskning. Ingen modelletikett kan på egen hand nå
     "övrigt" och därmed tystas bort.
  4. Modellen svarar fel, avstår eller är osäker -> "övrigt" + abstain + granskning,
     och konfidensen blir 0.00: den avser inte "sannolikt övrigt" utan "oklassat".
     Det avvisade förslaget ligger i proposed_label/proposed_confidence.
   5. Journalavvikelse går ALLTID till mänsklig granskning, även när reglerna var
     entydiga - fel patient och förväxling får aldrig tystas bort. Det gäller även de
     inloggningsärenden som är delar i ett större händelseförlopp (delad/stulen
     session, åtkomst nekad trots rätt behörighet): de kan få etiketten
     felaktig_inloggning men ska aldrig besvaras av lösenordsåterställning.
  6. Åtkomst och behörighet (obehörig läsning av annans journal, åtkomstlogg,
     logguppföljning) är informationssäkerhetsärenden enligt Patientdatalagen
     (2008:355) 4 kap., inte journalföring. Mönstren väger mot etiketten övrigt, men
     ärendet går ALLTID till mänsklig granskning med routing_hint
     "behörighetsansvarig" - det får aldrig tystas bort. Hänvisningen följer med även
     om poängen i ett blandat fall ger en annan etikett.
  7. Granskningsnotat appliceras sist. Beslutet och notatet hamnar i TVÅ filer:
     beslutet (vad verktyget föreslog) och en auditpost i schema.AUDIT_SCHEMA
     (vem, när, från vilken etikett till vilken, med maskerad kommentar).
"""
from __future__ import annotations

import json

from . import privacy, rules, schema
from .model import safe_call, stub_model

MODEL_MIN_CONFIDENCE = 0.70


def _decision(case_id, label, confidence, source, review, hits, rationale, trace,
              disagreement=False, extra=None, routing_hint=None):
    beslut = {"schema_version": schema.SCHEMA_VERSION, "case_id": case_id, "label": label,
              "label_display": schema.display_label(label),
              "confidence": round(float(confidence), 2), "source": source,
              "needs_human_review": review, "rule_hits": hits, "disagreement": disagreement,
              "rationale": rationale, "trace": trace}
    beslut.update(extra or {})
    if routing_hint:      # endast för åtkomst- och behörighetsärenden
        beslut["routing_hint"] = routing_hint
    return privacy.sanitize(beslut)


def motsatt(rr: dict, forslag: dict) -> bool:
    """Sant om modellens förslag pekar på en annan kategori än regelernas topp.

    Jämförelsen sker mot regelernas *topp*, inte mot en säker etikett: när reglerna
    är osäkra finns ingen säker etikett att jämföra med, men en granskare vill veta
    att de två spåren pekar åt olika håll.
    """
    return bool(rr["topp"]) and bool(forslag) and rr["topp"] != forslag["label"]


class TriageEngine:
    """Klassificerar inkommen text. Modellen är en inpluginje, se model.py."""

    def __init__(self, model_fn=None, model_min_confidence: float = MODEL_MIN_CONFIDENCE):
        if not 0.0 <= float(model_min_confidence) <= 1.0:
            raise ValueError("tröskeln måste ligga mellan 0 och 1")
        self.model_fn = stub_model if model_fn is None else model_fn
        self.model_min_confidence = float(model_min_confidence)

    def triage(self, text: str, case_id: str = "unknown") -> dict:
        if schema.validate(case_id, schema.CASE_ID):
            raise ValueError("ogiltigt case_id: {!r}".format(case_id))
        rå_text, kategorier = privacy.mask_text(text)
        trace = []
        if kategorier:
            trace.append("integritet: {} maskade före regler, modell och utdata: {}".format(
                ", ".join(kategorier), privacy.MASKER[kategorier[0]]))
        rr = rules.decide_rules(rå_text)
        trace.append("regler: topp={} poäng=[{}] marginal={} säker={}".format(
            schema.display_label(rr["topp"]), rules.beskriv_scores(rr), rr["margin"],
            rr["confident"]))
        if rr["undantag"]:
            trace.append("undantagsmönster (patient ringer/samtalar): bara vikt 3 vägs")
        if rr["confident"]:
            beslut = self._beslut_ur_regler(case_id, rr, trace)
        else:
            trace.append("reglerna är osäkra eller tysta -> modellsteget korsas")
            beslut = self._med_modell(rå_text, case_id, rr, trace)
        beslut["masked_identifiers"] = kategorier
        if rr["topp"]:
            # Reglernas topp även när de inte räckte för en säker etikett: det är den
            # andra sidan i en regel/modell-konflikt.
            beslut["rule_top_label"] = rr["topp"]
        fel = schema.validate_decision(beslut)
        if fel:
            raise ValueError("beslutet följer inte schemat: " + "; ".join(fel))
        return beslut

    def _beslut_ur_regler(self, case_id, rr, trace) -> dict:
        etikett = rr["label"]
        hint = rr["routing_hint"]
        alltid = etikett in rules.ALWAYS_REVIEW
        granska = alltid or rr["kräver_granskning"]
        if alltid:
            trace.append("{} kräver alltid mänsklig granskning, även med entydiga regler".format(
                schema.display_label(etikett)))
            rationale = ("Entydiga regler, men journalavvikelser granskas alltid av en "
                         "människa (utredning, åtgärd och uppföljning).")
        elif hint:
            trace.append("åtkomstmönster träffade: informationssäkerhets- och åtkomstkontroll"
                         "ärende enligt Patientdatalagen (2008:355) 4 kap., skickas till "
                         "{}".format(hint))
            rationale = ("Entydiga regler, men ärendet gäller behörighet, åtkomst eller "
                         "logguppföljning och ska handläggas av {} - inte som ett fel i "
                         "journalföringen.".format(hint))
        elif rr["kräver_granskning"]:
            trace.append("reglerna är entydiga, men ärendet gäller behörighet eller session "
                         "och ska alltid granskas av en människa")
            rationale = ("Entydiga regler, men ett behörighets- eller sessionsärende ska "
                         "alltid till behörighetsansvarig: lösenordsåterställning löser "
                         "det inte.")
        else:
            trace.append("modell hoppas över: reglerna är entydiga")
            rationale = ("Entydiga regler (marginal {} över nästkommande kategori); ingen "
                         "modellkostnad, ingen osäkerhet.".format(rr["margin"]))
        return _decision(case_id, etikett, rr["confidence"], "rules", granska, rr["hits"],
                         rationale, trace, routing_hint=hint)

    def _med_modell(self, text, case_id, rr, trace) -> dict:
        forslag, status = safe_call(self.model_fn, text)
        trace.append("modell: {}".format(status))
        extra = {"model_status": status}
        hint = rr["routing_hint"]      # följer med även när reglerna inte räckte
        if status == "ok":
            extra["proposed_label"] = forslag["label"]
            extra["proposed_confidence"] = round(float(forslag["confidence"]), 2)
            if forslag["confidence"] >= self.model_min_confidence:
                trace.append("förslaget godtas endast som förslag och kräver manuell granskning")
                return _decision(case_id, forslag["label"], forslag["confidence"], "modell_forslag",
                                 True, rr["hits"], "Modellförslag, inväntar granskning: "
                                 + forslag["rationale"], trace, motsatt(rr, forslag), extra,
                                 routing_hint=hint)
            orsak = "Modellförslaget var för osäkert (minst {})".format(self.model_min_confidence)
        else:
            orsak = "Modellen svarade inte användbart"
        trace.append("abstain -> 'Övrigt' skickas till manuell granskning, aldrig tystas bort")
        # Konfidensen blir 0.00: etiketten tvingades till ovrigt, den är inte "sannolik".
        return _decision(case_id, rules.OTHER, 0.0, "abstain", True, rr["hits"], orsak
                         + ". 'Övrigt' betyder här att processen inte kan hantera fallet "
                           "automatiskt.", trace, motsatt(rr, forslag), extra,
                         routing_hint=hint)


def audit_record(beslut: dict, notat: dict) -> dict | None:
    """Bygg en auditpost för ett mänskligt beslut, eller None om notatet är ogiltigt.

    Posten har eget schema och egen version och hamnar i en egen fil. Kommentaren
    maskeras här - granskarens fritext ska inte kunna smugla in ett namn eller ett
    personnummer i auditregistret.
    """
    if not notat or schema.validate(notat, schema.APPROVAL_SCHEMA):
        return None
    if schema.validate(beslut["case_id"], schema.CASE_ID) or \
            not isinstance(beslut.get("label"), str):
        return None
    post = {"schema_version": schema.AUDIT_SCHEMA_VERSION, "case_id": beslut["case_id"],
            "reviewer": notat["reviewer"], "timestamp": notat["reviewed_at"],
            "previous_label": beslut["label"], "new_label": notat["label"],
            "note_masked": privacy.mask_note(notat.get("note", ""))}
    return None if schema.validate_audit(post) else privacy.sanitize(post)


def apply_approvals(decisions: list[dict], approvals: dict) -> tuple[list[dict], list[dict]]:
    """Granskningsnotat skriver över automatiska beslut (sist i kedjan).

    Returnerar (beslut, audit). Besluten innehåller INTE granskarens uppgifter eller
    kommentar - de ligger i auditposterna, som skrivs till en separat fil. Ett beslut
    pekar ut sin auditpost med granskarens namn och tidpunkt, aldrig med notatets text.

    Ett notat utan granskare, tidpunkt eller giltig etikett ignoreras hellre än att
    krascha; notat för case_id som inte finns i beslutslistan appliceras aldrig.
    """
    ut = []
    audit = []
    for beslut in decisions:
        notat = approvals.get(beslut["case_id"]) if isinstance(approvals, dict) else None
        post = audit_record(beslut, notat)
        if post is None:  # ogiltigt notat ignoreras hellre än att krascha
            ut.append(beslut)
            continue
        kopia = dict(beslut)
        kopia.update({
            "label": post["new_label"], "label_display": schema.display_label(post["new_label"]),
            "confidence": 1.0, "source": "manuell", "needs_human_review": False,
            "trace": beslut["trace"] + ["mänskligt beslut av {} ({}) skrev över {} med etiketten "
                                        "{}".format(post["reviewer"], post["timestamp"],
                                                    beslut["source"],
                                                    schema.display_label(post["new_label"]))],
            "rationale": "Granskat av {} (se auditregistret, granskad {})".format(
                post["reviewer"], post["timestamp"])})
        ut.append(privacy.sanitize(kopia))
        audit.append(post)
    return ut, audit


def review_queue(decisions: list[dict]) -> list[dict]:
    return [d for d in decisions if d["needs_human_review"]]


def read_jsonl(path) -> list[dict]:
    """Läs JSONL. Trasig JSON pekas ut med radnummer i stället för ett tomt felmeddelande."""
    rader = []
    with open(path, encoding="utf-8") as handle:
        for num, rad in enumerate(handle, start=1):
            if not rad.strip():
                continue
            try:
                rader.append(json.loads(rad))
            except ValueError as fel:
                raise ValueError("rad {} i {} är inte giltig JSON: {}".format(num, path, fel))
    return rader


def write_jsonl(path, rows: list[dict]) -> None:
    """Skriv JSONL. allow_nan=False: icke-finita tal ska aldrig nå en fil."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n"
                             for r in rows))