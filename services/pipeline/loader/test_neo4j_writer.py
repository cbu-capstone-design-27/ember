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
        self.assertEqual(params["rows"][0]["hash"], "h")

    def test_embed_candidates_queries_each_source_backed_label_and_skips_blank_text(self):
        from loader.facts import SOURCE_BACKED

        row = {"uuid": "u1", "group_id": G, "key": "a#1", "text": "hello", "hash": None, "vector": None}
        driver = FakeDriver(rows=[[row]])
        found = Neo4jWriter("bolt://x", "u", "p", driver=driver).embed_candidates([G])
        self.assertEqual(len(driver.log), len(SOURCE_BACKED))
        for query, params in driver.log:
            self.assertEqual(params["gids"], [G])
            self.assertIn("n.group_id IN $gids", query)
            self.assertIn("n.embed_text IS NOT NULL", query)
            self.assertIn("trim(n.embed_text) <> ''", query)
        queried = sorted(q.split("MATCH (n:")[1].split(")")[0] for q, _ in driver.log)
        self.assertEqual(queried, sorted(SOURCE_BACKED))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0], {"label": sorted(SOURCE_BACKED)[0], **row})

    def test_reloading_a_node_never_sends_the_embedding_so_it_is_not_wiped(self):
        driver = FakeDriver()
        node = NodeRec("WorkItem", G, "a#1", {"source": "github", "kind": "issue", "external_id": "a#1",
                                              "embed_text": "hello"})
        Neo4jWriter("bolt://x", "u", "p", driver=driver).write_nodes([node])
        query, params = driver.log[0]
        # merges into existing properties, never replaces them
        self.assertIn("SET n += CASE WHEN newer OR coalesce(cur.deleted, false) = false THEN row.props", query)
        self.assertNotIn("SET n = ", query)
        self.assertNotIn("embedding", params["rows"][0]["props"])


class OrderingTests(unittest.TestCase):
    """EMBER-85: props apply by the source's occurred_at (the Cypher itself runs in test_pg_load.py
    and test_neo4j_integration.py against a real Neo4j)."""

    def test_rows_carry_occurred_at_only_when_set(self):
        driver = FakeDriver()
        w = Neo4jWriter("bolt://x", "u", "p", driver=driver)
        w.write_nodes([NodeRec("Person", G, "github:a", {"name": "A"}, occurred_at="2026-10-01T00:00:00.000000Z")])
        self.assertEqual(driver.log[0][1]["rows"][0]["occurred_at"], "2026-10-01T00:00:00.000000Z")
        for query in (node_query("WorkItem"), node_query("Person"), edge_query("AUTHORED", "Person", "WorkItem")):
            self.assertIn("row.occurred_at >= cur.occurred_at) AS newer", query)
            self.assertIn("SET n += CASE WHEN newer THEN {} ELSE cur END" if "(n:" in query
                          else "SET r += CASE WHEN newer THEN {} ELSE cur END", query)

    def test_one_element_appears_once_per_query_in_the_order_given(self):
        driver = FakeDriver()
        w = Neo4jWriter("bolt://x", "u", "p", driver=driver)
        props = {"source": "github", "kind": "issue", "external_id": "a#1"}
        first = NodeRec("WorkItem", G, "a#1", {**props, "status": "open"}, occurred_at="2026-10-02T00:00:00.000000Z")
        other = NodeRec("WorkItem", G, "a#2", {**props, "external_id": "a#2"}, occurred_at="2026-10-01T00:00:00.000000Z")
        second = NodeRec("WorkItem", G, "a#1", {**props, "status": "done"}, occurred_at="2026-10-03T00:00:00.000000Z")
        w.write_nodes([first, other, second])
        sent = [[row["props"].get("status") for row in params["rows"]] for _, params in driver.log]
        self.assertEqual(sent, [["open", None], ["done"]])

    def test_placeholders_merge_source_backed_endpoints_and_match_people(self):
        query = edge_query("AUTHORED", "Person", "Message", placeholders=True)
        self.assertIn("MATCH (a:Person {group_id: row.group_id, uuid: row.src})", query)
        self.assertIn("MERGE (b:Message {group_id: row.group_id, source: row.source, external_id: row.dst_key}) "
                      "ON CREATE SET b.uuid = row.dst, b.placeholder = true", query)
        self.assertNotIn("placeholder", edge_query("AUTHORED", "Person", "Message"))
        self.assertIn("REMOVE n.placeholder", node_query("Message"))

        driver = FakeDriver(rows=[[{"n": 1}]])
        edge = EdgeRec("REFERENCES", "t_jira", "Message", "10001", "WorkItem", "PHO-1")
        Neo4jWriter("bolt://x", "u", "p", driver=driver).write_edges([edge], placeholders=True)
        row = driver.log[0][1]["rows"][0]
        self.assertEqual((row["source"], row["src_key"], row["dst_key"]), ("jira", "10001", "PHO-1"))


if __name__ == "__main__":
    unittest.main()
