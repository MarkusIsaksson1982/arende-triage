"""Självdefinierat JSON-schema + minimal validator (endast stdlib).
Subset: type, enum, required, properties, additionalProperties, items, minimum, maximum,
minLength, minItems, pattern. Tal måste vara FINITE: NaN och ±Infinity passerar varken
minimum/maximum-jämförelsen eller json.dumps(..., allow_nan=False)."""
from __future__ import annotations

import math
import re

SCHEMA_VERSION = "1.3.0"
AUDIT_SCHEMA_VERSION = "1.0.0"
LOGIN = "felaktig_inloggning"
JOURNAL = "avvikelse_i_patientjournal"
OTHER = "ovrigt"
LABELS = [LOGIN, JOURNAL, OTHER]

# Maskerar etiketterna i klartext (nycklar, fixtures, CLI) men användarvänliga
# svenska etiketter i allt mänskligt läsbart utdata.
LABEL_DISPLAY = {LOGIN: "Felaktig inloggning", JOURNAL: "Avvikelse i patientjournal",
                  OTHER: "Övrigt"}

SOURCES = ["rules", "modell_forslag", "abstain", "manuell"]
MODEL_STATUS = ["ok", "abstain", "invalid_schema", "error"]
# Vem ett åtkomst- eller behörighetsärende ska handläggas av, se Patientdatalagen
# (2008:355) 4 kap. Hänvisningen är ett fält i beslutet, inte en etikett: etiketten
# ovrigt säger bara att ärendet inte hör till de tre som finns.
BEHÖRIGHETSANSVARIG = "behörighetsansvarig"
ROUTING_HINTS = [BEHÖRIGHETSANSVARIG]
CONF = {"type": "number", "minimum": 0.0, "maximum": 1.0}
VIKT = {"type": "number", "minimum": 0.0}      # regelvikter är 1-3, inte sannolikheter
TEXT = {"type": "string", "minLength": 1}
# case_id är ett internt ärendenummer, aldrig ett personuppgift: ASCII, kort, och
# inga mellanslag så att det går att använda som filnamn och som nyckel.
CASE_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_.:-]{1,64}$"}
ISO_TID = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
TIMESTAMP = {"type": "string", "pattern": ISO_TID}
MASKED_KATEGORIER = ["personnummer", "namn", "telefon", "epost", "identifierare",
                     "organisationsnummer"]
TYPES = {"object": (dict,), "array": (list,), "string": (str,), "integer": (int,),
         "number": (int, float), "boolean": (bool,)}


def display_label(label: str | None) -> str:
    """Svensk etikett för mänskligt utdata; okänd etikett visas som den är."""
    return LABEL_DISPLAY.get(label, label or "ingen etikett")


def enum(values) -> dict:
    return {"type": "string", "enum": values}


_HIT = {"type": "object", "additionalProperties": False, "required": ["label", "phrase", "weight"],
        "properties": {"label": enum(LABELS), "phrase": TEXT, "weight": VIKT,
                       "rule": {"type": "string"}}}

DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "case_id", "label", "label_display", "confidence", "source",
                 "needs_human_review", "rule_hits", "disagreement", "rationale", "trace"],
    "properties": {"schema_version": enum([SCHEMA_VERSION]), "case_id": CASE_ID,
                   "label": enum(LABELS), "label_display": TEXT,
                   "confidence": CONF, "source": enum(SOURCES), "rationale": TEXT,
                   "needs_human_review": {"type": "boolean"}, "disagreement": {"type": "boolean"},
                   "rule_hits": {"type": "array", "items": _HIT},
                   "trace": {"type": "array", "minItems": 1, "items": TEXT},
                   # vilka kategorier av identifierare som maskades i detta fall
                   "masked_identifiers": {"type": "array", "items": enum(MASKED_KATEGORIER)},
                   # modellsteget: vad som föreslogs, och vad som hände med det
                   "model_status": enum(MODEL_STATUS), "proposed_label": enum(LABELS),
                   "proposed_confidence": CONF,
# regelns topp även när reglerna inte räckte för en säker etikett
                    "rule_top_label": enum(LABELS),
                    # vem ärendet ska handläggas av (åtkomst- och behörighetsärenden)
                    "routing_hint": enum(ROUTING_HINTS)}}

# Modellens svar valideras innan det får påverka en beslutskedja.
MODEL_OUTPUT_SCHEMA = {"type": "object", "additionalProperties": False,
                       "required": ["label", "confidence", "rationale"],
                       "properties": {"label": enum(LABELS), "confidence": CONF, "rationale": TEXT}}

# Ett mänskligt beslut måste vara spårbart: vem, när, och (gärna) varför. Tidpunkten
# måste vara en ISO 8601-tid - "i går" är inte en spårbar tidpunkt.
APPROVAL_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": ["label", "reviewer", "reviewed_at"],
                   "properties": {"label": enum(LABELS), "reviewer": TEXT,
                                  "reviewed_at": TIMESTAMP, "note": {"type": "string"}}}

# Auditregistret för mänskliga beslut har EGEN version och EGET schema, och ligger i
# en egen fil - separat från besluten. Granskarens fritext skrivs aldrig i ett beslut.
AUDIT_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["schema_version", "case_id", "reviewer", "timestamp",
                             "previous_label", "new_label", "note_masked"],
                "properties": {"schema_version": enum([AUDIT_SCHEMA_VERSION]), "case_id": CASE_ID,
                               "reviewer": TEXT, "timestamp": TIMESTAMP,
                               "previous_label": enum(LABELS), "new_label": enum(LABELS),
                               "note_masked": {"type": "string"}}}


def validate(instance, schema, path: str = "$") -> list[str]:
    """Returnera en lista med fel; tom lista betyder giltigt."""
    expected = schema.get("type")
    if expected and not _type_ok(instance, expected):
        return ["{}: förväntade {}, fick {}".format(path, expected, type(instance).__name__)]
    fel = []
    if "enum" in schema and instance not in schema["enum"]:
        fel.append("{}: {!r} finns inte i {}".format(path, instance, schema["enum"]))
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            fel.append("{}: för kort (minLength {})".format(path, schema["minLength"]))
        if "pattern" in schema and not re.match(schema["pattern"], instance):
            fel.append("{}: {!r} matchar inte {}".format(path, instance, schema["pattern"]))
    if _is_num(instance):
        if not math.isfinite(instance):
            fel.append("{}: icke-finita tal (NaN/Infinity)".format(path))
        else:
            for key, op in (("minimum", "<"), ("maximum", ">")):
                if key in schema and (instance < schema[key] if op == "<" else instance > schema[key]):
                    fel.append("{}: {} {} {}".format(path, instance, op, schema[key]))
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        fel += ["{}: saknar '{}'".format(path, k) for k in schema.get("required", []) if k not in instance]
        if schema.get("additionalProperties") is False:
            fel += ["{}: okänt fält '{}'".format(path, k) for k in instance if k not in props]
        fel += [e for k, sub in props.items() if k in instance
                for e in validate(instance[k], sub, "{}.{}".format(path, k))]
    if isinstance(instance, list) and schema.get("items"):
        fel += ["{}: för få element".format(path) for _ in range(schema.get("minItems", 0) - len(instance))]
        fel += [e for i, item in enumerate(instance)
                for e in validate(item, schema["items"], "{}[{}]".format(path, i))]
    return fel


def _type_ok(value, expected: str) -> bool:
    """bool är inte ett tal i JSON - det är en egen typ."""
    return expected in TYPES and isinstance(value, TYPES[expected]) and not (
        isinstance(value, bool) and expected in ("integer", "number"))


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_decision(decision) -> list[str]:
    return validate(decision, DECISION_SCHEMA)


def validate_model_output(raw) -> list[str]:
    return validate(raw, MODEL_OUTPUT_SCHEMA)


def validate_approval(raw) -> list[str]:
    return validate(raw, APPROVAL_SCHEMA)


def validate_audit(record) -> list[str]:
    return validate(record, AUDIT_SCHEMA)