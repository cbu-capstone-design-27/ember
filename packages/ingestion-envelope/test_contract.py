"""Contract test: each golden fixture is {type, body} raw intake."""

from __future__ import annotations

import copy
import unittest

from validate import SOURCES, assert_valid, load_fixtures, load_schema, validate


class IntakeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.fixtures = load_fixtures()

    def test_one_fixture_per_source(self):
        self.assertEqual(set(self.fixtures), set(SOURCES))

    def test_each_fixture_matches_schema(self):
        for source, fixture in self.fixtures.items():
            with self.subTest(source=source):
                self.assertEqual(fixture["type"], source)
                self.assertIsInstance(fixture["body"], dict)
                self.assertTrue(fixture["body"])
                assert_valid(fixture, self.schema)

    def test_rejects_missing_type(self):
        broken = copy.deepcopy(self.fixtures["github"])
        del broken["type"]
        errors = validate(broken, self.schema)
        self.assertTrue(any("type" in error for error in errors))

    def test_rejects_missing_body(self):
        broken = copy.deepcopy(self.fixtures["jira"])
        del broken["body"]
        errors = validate(broken, self.schema)
        self.assertTrue(any("body" in error for error in errors))

    def test_rejects_unknown_type(self):
        broken = copy.deepcopy(self.fixtures["slack"])
        broken["type"] = "email"
        errors = validate(broken, self.schema)
        self.assertTrue(errors)

    def test_rejects_non_object_body(self):
        for bad in ("raw text", ["not", "an", "object"], None, 1):
            with self.subTest(body=bad):
                broken = copy.deepcopy(self.fixtures["gitlab"])
                broken["body"] = bad
                self.assertTrue(validate(broken, self.schema))


if __name__ == "__main__":
    unittest.main()
