import tempfile
import unittest
from pathlib import Path

from loader.memory_writer import MemoryWriter
from loader.neo4j_writer import Neo4jWriter
from loader.run import run
from loader.snapshot import make_ref
from loader.test_neo4j_writer import FakeDriver
from loader.testdata import write_sample


class MemorySnapshotTests(unittest.TestCase):
    def test_sample_snapshot_has_refs_edges_and_no_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            writer = MemoryWriter()
            run("brandon-dev", Path(tmp) / "raw", writer, Path(tmp) / "r.jsonl")
        for node in writer.nodes.values():
            if node.get("external_id") == "acme/widget#42":
                node["embedding"], node["embedding_text_hash"] = [0.1], "h"
        snap = writer.snapshot(["brandon-dev_github", "brandon-dev_jira", "brandon-dev_slack"])
        refs = {n["ref"] for n in snap.nodes}
        self.assertIn("github:acme/widget#42", refs)
        self.assertIn("jira:EMBER-39", refs)
        self.assertIn("slack:C012AB3CD", refs)
        issue = next(n for n in snap.nodes if n["ref"] == "github:acme/widget#42")
        self.assertEqual((issue["label"], issue["props"]["status"]), ("WorkItem", "open"))
        self.assertNotIn("embedding", issue["props"])
        self.assertNotIn("embedding_text_hash", issue["props"])
        self.assertIn({"type": "RESOLVES", "src_label": "Change", "src": "github:acme/widget#43",
                       "dst_label": "WorkItem", "dst": "github:acme/widget#42"}, snap.edges)

    def test_other_tenants_are_not_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            writer = MemoryWriter()
            run("brandon-dev", Path(tmp) / "raw", writer, Path(tmp) / "r.jsonl")
        self.assertEqual(writer.snapshot(["someone-else_github"]).nodes, [])

    def test_make_ref(self):
        self.assertEqual(make_ref("jira", "EMBER-39"), "jira:EMBER-39")


class Neo4jSnapshotTests(unittest.TestCase):
    def test_queries_and_row_mapping(self):
        nodes = [{"label": "WorkItem", "props": {"source": "github", "external_id": "a#1", "embedding": None,
                                                 "status": "open"}}]
        edges = [{"type": "CONTAINS", "src_label": "Container", "src_source": "github", "src_id": "a",
                  "dst_label": "WorkItem", "dst_source": "github", "dst_id": "a#1"}]
        driver = FakeDriver(rows=[nodes, edges])
        snap = Neo4jWriter("bolt://x", "u", "p", driver=driver).snapshot(["t_github"])
        self.assertIn("external_id IS NOT NULL", driver.log[0][0])
        self.assertEqual(driver.log[0][1]["gids"], ["t_github"])
        self.assertEqual(snap.nodes, [{"label": "WorkItem", "ref": "github:a#1", "source": "github",
                                       "props": {"source": "github", "external_id": "a#1", "status": "open"}}])
        self.assertEqual(snap.edges, [{"type": "CONTAINS", "src_label": "Container", "src": "github:a",
                                       "dst_label": "WorkItem", "dst": "github:a#1"}])


if __name__ == "__main__":
    unittest.main()
