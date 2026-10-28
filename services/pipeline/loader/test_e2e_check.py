"""The EMBER-86 end-to-end check, without Postgres or Neo4j: the expected file, the comparison and the queries.

The run against Postgres (MemoryWriter, and Neo4j when NEO4J_TEST_URI is set) is in test_pg_load.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
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

    def test_every_query_parameter_is_supplied(self):
        specs = committed()["queries"]
        self.assertEqual(len(specs), 5)
        for spec in specs:
            used = set(re.findall(r"\$(\w+)", spec["cypher"]))
            self.assertLessEqual(used, {"jira", "slack", "github"} | set(spec["params"]), spec["id"])


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


class FakeGraph:
    def __init__(self, rows):
        self.rows, self.seen = rows, []

    def _run(self, cypher, **params):
        self.seen.append(params)
        return self.rows


class QueryTest(unittest.TestCase):
    spec = {"id": "q", "question": "who?", "cypher": "RETURN 1", "params": {"issue": "PHO-1"},
            "expect": [{"person": "B"}, {"person": "A"}]}

    def test_rows_compare_without_regard_to_order_unless_the_query_is_ordered(self):
        graph = FakeGraph([{"person": "A"}, {"person": "B"}])
        self.assertTrue(e2e_check.run_queries(graph, "t", [self.spec])[0].ok)
        self.assertFalse(e2e_check.run_queries(graph, "t", [{**self.spec, "ordered": True}])[0].ok)
        self.assertEqual(graph.seen[0], {"jira": "t_jira", "slack": "t_slack", "github": "t_github", "issue": "PHO-1"})

    def test_a_different_answer_is_a_failure(self):
        [result] = e2e_check.run_queries(FakeGraph([{"person": "A"}]), "t", [self.spec])
        self.assertFalse(result.ok)
        self.assertTrue(result.ran)

    def test_the_tenant_fills_expected_group_names(self):
        spec = {**self.spec, "expect": [{"grp": "{tenant}_jira"}]}
        [result] = e2e_check.run_queries(FakeGraph([{"grp": "acme_jira"}]), "acme", [spec])
        self.assertTrue(result.ok)

    def test_smoke_queries_report_only_numbers_and_judge_them(self):
        """Real-data mode: six shapes, no known answers; a row of counts passes or fails, text never shows."""
        rows = [{"issues_with_comments": 2, "commenters": 1, "comments": 3},
                {"threads": 2, "largest_thread": 3, "messages": 4, "messages_with_sender": 4},
                {"issues": 5, "linked_issues": 2, "placeholders": 0},
                {"deleted": 1, "still_holding_content": 0},
                {"nodes": 9, "wrong_source": 0, "crossing": 0},
                {"changes": 5, "pull_requests": 2, "commits": 2, "placeholders": 1, "with_author": 4, "reviewed": 1,
                 "with_comments": 2, "touching_modules": 3}]

        class InOrder:
            def __init__(self, rows):
                self.rows = list(rows)

            def _run(self, cypher, **params):
                return [self.rows.pop(0)]

        self.assertEqual(len(e2e_check.SMOKE_QUERIES), 6)
        results = e2e_check.run_smoke_queries(InOrder(rows), "t")
        self.assertTrue(all(r.ok for r in results), [r.id for r in results if not r.ok])
        results = e2e_check.run_smoke_queries(InOrder(rows[:4] + [dict(rows[4], crossing=1)] + rows[5:]), "t")
        self.assertEqual([r.id for r in results if not r.ok], ["subgraph-isolation"])
        result = e2e_check.Result("t", queries=results)
        self.assertIn("query subgraph-isolation: smoke check failed", result.failures)
        self.assertNotIn("expected", json.dumps(result.to_json()["queries"][0]))

    def test_the_report_of_a_tenant_without_an_expected_file_has_no_message_text(self):
        writer = MemoryWriter()
        writer.nodes[("Message", "u1")] = {"group_id": "t_slack", "uuid": "u1", "source": "slack", "kind": "message",
                                           "external_id": "C1:1", "embed_text": "SECRET WORDS", "name": "Some Person"}
        text = e2e_check.verify(writer, "t", None).render()
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Some Person", text)
        self.assertNotIn("C1:1", text)

    def test_a_memory_graph_skips_the_queries(self):
        [result] = e2e_check.run_queries(MemoryWriter(), "t", [self.spec])
        self.assertFalse(result.ran)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
