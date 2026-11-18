import tempfile
import unittest
from pathlib import Path

from loader.memory_writer import MemoryWriter
from loader.run import run
from loader.snapshot import make_ref
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


if __name__ == "__main__":
    unittest.main()
