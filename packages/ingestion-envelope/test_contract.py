"""Contract test: each golden fixture matches the v1 ingestion envelope."""

from __future__ import annotations

import copy
import unittest

from validate import SOURCES, assert_valid, load_fixtures, load_schema, validate


class EnvelopeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.fixtures = load_fixtures()

    def test_one_fixture_per_source(self):
        self.assertEqual(set(self.fixtures), set(SOURCES))

    def test_each_fixture_matches_schema(self):
        for source, fixture in self.fixtures.items():
            with self.subTest(source=source):
                self.assertEqual(fixture["source"]["system"], source)
                assert_valid(fixture, self.schema)
                self.assertNotIn("embedding", fixture)
                self.assertNotIn("embeddings", fixture)

    def test_native_ids_stay_in_the_envelope(self):
        native = {
            "github": "acme/widget#42",
            "jira": "EMBER-39",
            "slack": "C012AB3CD:1717000001.000200",
            "teams": "1699900000000",
            "gitlab": "acme/widget!7",
        }
        for source, expected in native.items():
            self.assertEqual(self.fixtures[source]["identity"]["source_native_id"], expected)

    def test_rejects_missing_identity_and_top_level_embedding(self):
        broken = copy.deepcopy(self.fixtures["github"])
        del broken["identity"]["source_native_id"]
        broken["embedding"] = [0.1, 0.2]
        errors = validate(broken, self.schema)
        self.assertTrue(any("source_native_id" in error for error in errors))
        self.assertTrue(any("embedding" in error for error in errors))

    def test_rejects_payload_from_the_wrong_source(self):
        mixed = copy.deepcopy(self.fixtures["jira"])
        mixed["payload"] = {"repository": "acme/widget", "number": 1, "title": "nope"}
        errors = validate(mixed, self.schema)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
