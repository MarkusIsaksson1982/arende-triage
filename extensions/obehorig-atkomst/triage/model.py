"""Modellsteget är en utbytbar funktion: model_fn(text) -> dict | None.

Standardkopplingen är en offline-stub som läser sparade fixtures
(data/fixtures.json): ingen nätverkskoppling, deterministisk, och den kan AVSTÅ.
Saknas en fixture returneras None - det osäkra utfallet, som skickas till
mänsklig granskning istället för gissning.

VIKTIGT: texten som skickas hit är alltid redan maskerad av triage.privacy - personnummer,
namn, telefon, e-post och andra identifierare är utbytta mot mask-token före anropet, så
stubben och en riktig klient ser aldrig en direkt identifierare i klartext. En riktig
klient måste dessutom ha en dokumenterad avtalsgrund - se README om Patientdatalagen.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import schema

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "data" / "fixtures.json"


def text_key(text: str) -> str:
    """Stabil fixture-nyckel: hash över normaliserad text, inte case_id.

    Modellen får bara se texten. Vore nyckeln case_id hade testerna blivit lata,
    men svaret hade blivit odrift.
    """
    return hashlib.sha256(" ".join(text.split()).lower().encode("utf-8")).hexdigest()[:16]


def load_fixtures(path: Path | None = None) -> dict:
    path = Path(path) if path else FIXTURE_PATH
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def stub_model(text: str, fixtures: dict | None = None) -> dict | None:
    """Deterministisk offline-ersättning för en språkmodell."""
    entry = (fixtures if fixtures is not None else load_fixtures()).get(text_key(text))
    return dict(entry) if entry else None


def safe_call(model_fn, text: str) -> tuple[dict | None, str]:
    """Kalla modellen och validera svaret. Status: ok | abstain | invalid_schema | error."""
    if model_fn is None:
        return None, "abstain"
    try:
        raw = model_fn(text)
    except Exception:
        return None, "error"
    if raw is None:
        return None, "abstain"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None, "invalid_schema"
    fel = schema.validate(raw, schema.MODEL_OUTPUT_SCHEMA)
    return (raw, "ok") if not fel else (None, "invalid_schema")