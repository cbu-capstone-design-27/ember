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

    def test_identity_is_created_never_overwritten(self):
        from ontology.config import EmbeddingIdentity

        driver = FakeDriver()
        w = Neo4jWriter("bolt://x", "u", "p", driver=driver)
        w.write_embedding_identity(EmbeddingIdentity("m", "r", 1024, "{query}"))
        query, params = driver.log[0]
        self.assertIn("MERGE (c:EmberConfig {key: 'embedding'}) ON CREATE SET", query)
        self.assertNotIn("ON MATCH", query)
        self.assertEqual((params["model"], params["dimension"]), ("m", 1024))

    def test_read_identity_returns_none_when_absent_and_the_record_when_present(self):
        from ontology.config import EmbeddingIdentity

        self.assertIsNone(Neo4jWriter("bolt://x", "u", "p", driver=FakeDriver(rows=[[]])).read_embedding_identity())
        row = {"model": "m", "revision": "r", "dimension": 8, "query_template": "{query}"}
        got = Neo4jWriter("bolt://x", "u", "p", driver=FakeDriver(rows=[[row]])).read_embedding_identity()
        self.assertEqual(got, EmbeddingIdentity("m", "r", 8, "{query}"))

    def test_write_embeddings_sets_vector_and_hash_by_label(self):
        driver = FakeDriver()
        Neo4jWriter("bolt://x", "u", "p", driver=driver).write_embeddings(
            [{"label": "WorkItem", "uuid": "u1", "group_id": G, "vector": [0.5], "hash": "h"}])
        query, params = driver.log[0]
        self.assertIn("MATCH (n:WorkItem", query)
        self.assertIn("SET n.embedding = row.vector", query)
        self.assertEqual(params["rows"][0]["vector"], [0.5])

    def test_reloading_a_node_never_sends_the_embedding_so_it_is_not_wiped(self):
        driver = FakeDriver()
        node = NodeRec("WorkItem", G, "a#1", {"source": "github", "kind": "issue", "external_id": "a#1",
                                              "embed_text": "hello"})
        Neo4jWriter("bolt://x", "u", "p", driver=driver).write_nodes([node])
        query, params = driver.log[0]
        self.assertIn("SET n += row.props", query)  # merges into existing properties, never replaces them
        self.assertNotIn("embedding", params["rows"][0]["props"])


if __name__ == "__main__":
    unittest.main()
