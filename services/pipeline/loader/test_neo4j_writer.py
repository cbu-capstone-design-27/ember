import unittest

from loader.neo4j_writer import Neo4jWriter, edge_query, node_query
from loader.records import EdgeRec, NodeRec

G = "t_github"


class FakeSession:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.log.append((query, params))
        return self

    def data(self):
        return self.rows.pop(0) if self.rows else []


class FakeDriver:
    def __init__(self, rows=None):
        self.log, self.rows = [], rows or []

    def session(self, **kwargs):
        return FakeSession(self.log, self.rows)

    def close(self):
        pass


class QueryTests(unittest.TestCase):
    def test_queries_use_only_whitelisted_names(self):
        self.assertIn("MERGE (n:WorkItem {group_id: row.group_id, source: row.source, external_id: row.external_id})",
                      node_query("WorkItem"))
        self.assertIn("SET n.uuid = row.uuid", node_query("WorkItem"))
        self.assertIn("MERGE (n:Person {group_id: row.group_id, uuid: row.uuid})", node_query("Person"))
        self.assertNotIn("external_id", node_query("Person"))
        self.assertIn("(a)-[r:AUTHORED]->(b)", edge_query("AUTHORED", "Person", "WorkItem"))
        with self.assertRaises(ValueError):
            node_query("WorkItem) DETACH DELETE (n")
        with self.assertRaises(ValueError):
            edge_query("AUTHORED]->() DELETE x //", "Person", "WorkItem")


class WriterTests(unittest.TestCase):
    def test_write_nodes_groups_by_label(self):
        driver = FakeDriver()
        w = Neo4jWriter("bolt://x", "u", "p", driver=driver)
        n1 = NodeRec("WorkItem", G, "a#1", {"source": "github", "kind": "issue", "external_id": "a#1"})
        n2 = NodeRec("Container", G, "a", {"source": "github", "kind": "repository", "external_id": "a"})
        self.assertEqual(w.write_nodes([n1, n2]), 2)
        labels = sorted(q.split("MERGE (n:")[1].split(" ")[0] for q, _ in driver.log)
        self.assertEqual(labels, ["Container", "WorkItem"])
        rows = next(p["rows"] for q, p in driver.log if "n:WorkItem" in q)
        self.assertEqual(rows[0]["uuid"], n1.uuid)
        self.assertEqual(rows[0]["props"]["external_id"], "a#1")
        self.assertEqual((rows[0]["source"], rows[0]["external_id"]), ("github", "a#1"))

    def test_person_rows_carry_no_source_key_and_merge_on_uuid(self):
        driver = FakeDriver()
        w = Neo4jWriter("bolt://x", "u", "p", driver=driver)
        person = NodeRec("Person", G, "github:octocat", {"name": "Octo"})
        w.write_nodes([person])
        query, params = driver.log[0]
        self.assertIn("{group_id: row.group_id, uuid: row.uuid}", query)
        self.assertEqual(params["rows"], [{"group_id": G, "uuid": person.uuid, "props": {"name": "Octo"}}])

    def test_source_backed_node_without_a_schema_key_is_refused(self):
        w = Neo4jWriter("bolt://x", "u", "p", driver=FakeDriver())
        with self.assertRaisesRegex(ValueError, "external_id"):
            w.write_nodes([NodeRec("WorkItem", G, "a#1", {"source": "github", "kind": "issue"})])

    def test_write_edges_sums_the_counts_neo4j_returns(self):
        driver = FakeDriver(rows=[[{"n": 2}]])
        w = Neo4jWriter("bolt://x", "u", "p", driver=driver)
        edges = [EdgeRec("CONTAINS", G, "Container", "a", "WorkItem", f"a#{i}") for i in range(3)]
        self.assertEqual(w.write_edges(edges), 2)

    def test_reset_is_scoped_by_group_id(self):
        driver = FakeDriver()
        Neo4jWriter("bolt://x", "u", "p", driver=driver).reset(["t_github", "t_jira"])
        query, params = driver.log[0]
        self.assertIn("n.group_id IN $gids", query)
        self.assertEqual(params["gids"], ["t_github", "t_jira"])

    def test_ensure_schema_applies_constraints_and_lookup_indexes(self):
        driver = FakeDriver()
        Neo4jWriter("bolt://x", "u", "p", driver=driver).ensure_schema()
        queries = [q for q, _ in driver.log]
        self.assertEqual(sum(q.startswith("CREATE CONSTRAINT") for q in queries), 7)
        self.assertEqual(sum(q.startswith("CREATE INDEX") for q in queries), 9)


if __name__ == "__main__":
    unittest.main()
