import json
import tempfile
import unittest
from pathlib import Path

from loader.reader import read_envelopes
from loader.records import EdgeRec, NodeRec, node_uuid


class RecordTests(unittest.TestCase):
    def test_uuid_is_deterministic_and_scoped(self):
        a = node_uuid("t_github", "WorkItem", "acme/w#1")
        self.assertEqual(a, node_uuid("t_github", "WorkItem", "acme/w#1"))
        self.assertNotEqual(a, node_uuid("t_jira", "WorkItem", "acme/w#1"))
        self.assertNotEqual(a, node_uuid("t_github", "Change", "acme/w#1"))

    def test_none_props_are_dropped(self):
        node = NodeRec("WorkItem", "t_github", "k", {"name": None, "status": "open"})
        self.assertEqual(node.props, {"status": "open"})
        edge = EdgeRec("RELATES_TO", "g", "WorkItem", "a", "WorkItem", "b", {"link_type": None})
        self.assertEqual(edge.props, {})
        self.assertEqual(edge.src_uuid, node_uuid("g", "WorkItem", "a"))


class ReaderTests(unittest.TestCase):
    def test_reads_good_and_flags_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "github").mkdir()
            good = {"type": "github", "body": {"number": 1}}
            (root / "github" / "a.jsonl").write_text(
                json.dumps(good) + "\n\n{not json\n" + json.dumps({"type": "github"}) + "\n",
                encoding="utf-8",
            )
            lines = list(read_envelopes(root))
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].envelope, good)
        self.assertIsNone(lines[0].error)
        self.assertEqual(lines[0].line, 1)
        self.assertIn("invalid JSON", lines[1].error)
        self.assertEqual(lines[1].line, 3)
        self.assertIn("body", lines[2].error)

    def test_skips_parts_directory_and_non_jsonl_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create a file in a .parts directory that should be skipped
            (root / "foo.jsonl.parts").mkdir()
            (root / "foo.jsonl.parts" / "C1.jsonl").write_text(
                "{not json\n",
                encoding="utf-8",
            )
            # Create a good file to ensure read_envelopes still works
            good = {"type": "github", "body": {"number": 1}}
            (root / "good.jsonl").write_text(
                json.dumps(good) + "\n",
                encoding="utf-8",
            )
            lines = list(read_envelopes(root))
        # Should only get the good file, not the one in .parts
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].envelope, good)
        self.assertIsNone(lines[0].error)

    def test_parent_folder_named_parts_does_not_hide_the_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "x.parts" / "raw"
            (root / "github").mkdir(parents=True)
            good = {"type": "github", "body": {"number": 1}}
            (root / "github" / "a.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
            lines = list(read_envelopes(root))
        self.assertEqual([line.envelope for line in lines], [good])


if __name__ == "__main__":
    unittest.main()
