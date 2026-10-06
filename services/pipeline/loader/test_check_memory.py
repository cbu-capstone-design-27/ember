import unittest

from loader.check import check_graph
from loader.facts import GraphFacts, expected_constraint_names, parse_schema_statements, SCHEMA_PATH
from loader.memory_writer import MemoryWriter
from loader.records import EdgeRec, NodeRec

G = "t_github"


def item(key="a#1"):
    return NodeRec("WorkItem", G, key, {"source": "github", "kind": "issue", "external_id": key})


def facts(**overrides):
    base = dict(labels={"WorkItem"}, relationships={}, constraints=expected_constraint_names(),
                missing_required={}, node_counts={"WorkItem": 1})
    base.update(overrides)
    return GraphFacts(**base)


class SchemaParsingTests(unittest.TestCase):
    def test_statements_and_names_come_from_schema_cypher(self):
        statements = parse_schema_statements(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(statements), 7)
        self.assertTrue(all(s.startswith("CREATE CONSTRAINT") for s in statements))
        self.assertEqual(len(expected_constraint_names()), 7)
        self.assertIn("workitem_source_key", expected_constraint_names())


class CheckGraphTests(unittest.TestCase):
    def test_clean_graph_has_no_problems(self):
        rels = {("Container", "CONTAINS", "WorkItem"): 3}
        self.assertEqual(check_graph(facts(relationships=rels)), [])

    def test_each_kind_of_mismatch_is_reported(self):
        problems = check_graph(facts(
            labels={"WorkItem", "Ticket"},
            relationships={("WorkItem", "AUTHORED", "Person"): 2, ("Person", "LIKES", "WorkItem"): 1},
            constraints=set(),
            missing_required={"WorkItem": 4},
        ))
        text = "\n".join(problems)
        self.assertIn("unexpected node label Ticket", text)
        self.assertIn("AUTHORED not allowed from WorkItem to Person (2 found)", text)
        self.assertIn("unknown relationship type LIKES", text)
        self.assertIn("missing constraint workitem_source_key", text)
        self.assertIn("4 WorkItem nodes are missing source, kind or external_id", text)


class MemoryWriterTests(unittest.TestCase):
    def test_merge_is_idempotent_and_edges_need_both_endpoints(self):
        w = MemoryWriter()
        w.write_nodes([item("a#1"), NodeRec("Container", G, "a", {"source": "github", "kind": "repository", "external_id": "a"})])
        w.write_nodes([item("a#1")])
        self.assertEqual(len(w.nodes), 2)
        good = EdgeRec("CONTAINS", G, "Container", "a", "WorkItem", "a#1")
        dangling = EdgeRec("CONTAINS", G, "Container", "a", "WorkItem", "a#9")
        self.assertEqual(w.write_edges([good, dangling]), 1)
        self.assertEqual(w.write_edges([good]), 1)
        self.assertEqual(len(w.edges), 1)

    def test_props_merge_without_erasing(self):
        w = MemoryWriter()
        w.write_nodes([NodeRec("Container", G, "c", {"source": "slack", "kind": "channel", "external_id": "c", "name": "dev"})])
        w.write_nodes([NodeRec("Container", G, "c", {"source": "slack", "kind": "channel", "external_id": "c"})])
        self.assertEqual(next(iter(w.nodes.values()))["name"], "dev")

    def test_facts_and_reset_are_scoped_to_group_ids(self):
        w = MemoryWriter()
        w.write_nodes([item("a#1"), NodeRec("WorkItem", "other_github", "z", {"source": "github", "kind": "issue", "external_id": "z"})])
        self.assertEqual(w.read_facts([G]).node_counts, {"WorkItem": 1})
        w.reset([G])
        self.assertEqual(len(w.nodes), 1)

    def test_missing_required_is_counted(self):
        w = MemoryWriter()
        w.write_nodes([NodeRec("WorkItem", G, "a#1", {"source": "github"})])
        self.assertEqual(w.read_facts([G]).missing_required, {"WorkItem": 1})


if __name__ == "__main__":
    unittest.main()
