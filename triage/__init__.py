"""Offentligt API: from triage import triage_case, TriageEngine."""
from .matning import rapport, utvardera  # noqa: F401
from .pipeline import (TriageEngine, apply_approvals, audit_record, read_jsonl,  # noqa: F401
                       review_queue, write_jsonl)
from .privacy import find_personnummer, mask_note, mask_text  # noqa: F401
from .schema import DECISION_SCHEMA, AUDIT_SCHEMA, SCHEMA_VERSION, display_label  # noqa: F401

__all__ = ["TriageEngine", "triage_case", "apply_approvals", "audit_record", "review_queue",
           "read_jsonl", "write_jsonl", "utvardera", "rapport", "mask_text", "mask_note",
           "find_personnummer", "display_label", "DECISION_SCHEMA", "AUDIT_SCHEMA",
           "SCHEMA_VERSION"]


def triage_case(text: str, case_id: str = "unknown", model_fn=None) -> dict:
    """Snabbgenomgång för ett enskilt fall (stub-modell som standard)."""
    return TriageEngine(model_fn=model_fn).triage(text, case_id)