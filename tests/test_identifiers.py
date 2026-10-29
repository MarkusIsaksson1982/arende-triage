"""Swedish personnummer and organisationsnummer detection and masking (PII redaction before the LLM).

Fixtures: data/identifier_fixtures.json - all numbers are synthetic, Luhn-valid where they should be.
"""
import json
import unittest
from datetime import date
from pathlib import Path

from triage import find_organisationsnummer, schema, triage_case
from triage.privacy import mask_text

FIXTURES = json.loads((Path(__file__).resolve().parent.parent / "data" / "identifier_fixtures.json").read_text())
IDAG = date(2026, 10, 29)


class TestIdentifierFixtures(unittest.TestCase):
    def test_mask_swedish_identifiers_fixtures(self):
        for f in FIXTURES:
            with self.subTest(f["id"]):
                maskad, kategorier = mask_text(f["text"], IDAG)
                self.assertEqual(maskad, f["maskad"])
                self.assertEqual(kategorier, f["kategorier"])


class TestOrganisationsnummer(unittest.TestCase):
    def test_detect_organisationsnummer_with_luhn(self):
        self.assertEqual(find_organisationsnummer("559901-1235 och 559901-1234"), ["559901-1235"])

    def test_twelve_digit_16_prefix(self):
        self.assertEqual(find_organisationsnummer("org 168024007778"), ["168024007778"])

    def test_third_digit_below_two_is_not_orgnr(self):
        # 870312-4514 is a valid personnummer; month digit < 2 means it is not an organisation number
        self.assertEqual(find_organisationsnummer("870312-4514"), [])

    def test_part_of_longer_number_is_ignored(self):
        self.assertEqual(find_organisationsnummer("1559901123599"), [])

    def test_orgnr_masked_in_triage_decision(self):
        beslut = triage_case("Leverantör 559901-1235 kan inte logga in, åtkomst nekad.", "syn-org")
        self.assertIn("organisationsnummer", beslut["masked_identifiers"])
        self.assertNotIn("559901-1235", json.dumps(beslut, ensure_ascii=False))

    def test_schema_lists_category(self):
        self.assertIn("organisationsnummer", schema.MASKED_KATEGORIER)
        self.assertEqual(schema.SCHEMA_VERSION, "1.3.0")


if __name__ == "__main__":
    unittest.main()
