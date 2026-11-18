"""The EMBER-86 end-to-end check, without Postgres: the expected file and the comparison.

The run against Postgres is in test_pg_load.py.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from . import e2e_check
from .facts import GraphFacts
from .memory_writer import MemoryWriter

PHOTON = Path(__file__).resolve().parents[1] / "fixtures" / "photon"


def derive_module():
    spec = importlib.util.spec_from_file_location("derive_expected", PHOTON / "derive_expected.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def committed() -> dict:
    return json.loads((PHOTON / "expected-graph.json").read_text(encoding="utf-8"))


class ExpectedFileTest(unittest.TestCase):
    def test_the_committed_expected_file_is_what_the_fixtures_derive(self):
        self.assertEqual(committed(), derive_module().build(), "run fixtures/photon/derive_expected.py")

    def test_totals_agree_with_the_counts_the_fixture_readme_states(self):
        readme = (PHOTON / "README.md").read_text(encoding="utf-8")
        self.assertIn("112 issues (8 epics)", readme)
        self.assertIn("29 comments", readme)
        self.assertIn("637 messages and replies", readme)
        jira, slack = committed()["subgraphs"]["jira"]["nodes"], committed()["subgraphs"]["slack"]["nodes"]
        self.assertEqual((jira["WorkItem"], jira["Message"], slack["Message"], slack["Container"]), (112, 29, 637, 5))


class CompareTest(unittest.TestCase):
    def facts(self, nodes, rels):
        facts = GraphFacts()
        facts.node_counts = dict(nodes)
        facts.relationships = dict(rels)
        return facts

    def test_every_label_and_relationship_is_a_row_and_differences_are_flagged(self):
        expected = {"nodes": {"Message": 2, "Person": 1}, "relationships": {"Person AUTHORED Message": 2}}
        facts = self.facts({"Message": 2, "Container": 1}, {("Person", "AUTHORED", "Message"): 3})
        rows = {r.what: r for r in e2e_check.compare_counts("t_slack", expected, facts)}
        self.assertTrue(rows["node Message"].ok)
        self.assertFalse(rows["node Person"].ok)  # expected 1, none loaded
        self.assertFalse(rows["node Container"].ok)  # loaded, not expected
        edge = rows["edge Person AUTHORED Message"]
        self.assertEqual((edge.expected, edge.actual), (2, 3))

    def test_without_an_expected_file_counts_are_reported_not_compared(self):
        facts = self.facts({"WorkItem": 4}, {("Message", "REFERENCES", "WorkItem"): 1})
        rows = e2e_check.compare_counts("t_jira", None, facts)
        self.assertEqual([(r.what, r.expected, r.actual, r.ok) for r in rows],
                         [("edge Message REFERENCES WorkItem", None, 1, True), ("node WorkItem", None, 4, True)])

    def test_a_schema_mismatch_fails_the_result_even_without_an_expected_file(self):
        writer = MemoryWriter()
        writer.nodes[("Message", "u1")] = {"group_id": "t_slack", "uuid": "u1"}  # no source, kind, external_id
        result = e2e_check.verify(writer, "t", None)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(any("missing source, kind or external_id" in f for f in result.failures), result.failures)
        self.assertIn("no expected-counts file", result.render())

    def test_the_report_of_a_tenant_without_an_expected_file_has_no_message_text(self):
        writer = MemoryWriter()
        writer.nodes[("Message", "u1")] = {"group_id": "t_slack", "uuid": "u1", "source": "slack", "kind": "message",
                                           "external_id": "C1:1", "embed_text": "SECRET WORDS", "name": "Some Person"}
        text = e2e_check.verify(writer, "t", None).render()
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Some Person", text)
        self.assertNotIn("C1:1", text)


if __name__ == "__main__":
    unittest.main()
