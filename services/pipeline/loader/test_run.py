import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loader.memory_writer import MemoryWriter
from loader.reader import Line
from loader.run import InputError, run
from loader.testdata import JIRA, write_sample

FIXTURES = Path(__file__).resolve().parents[3] / "packages" / "ingestion-envelope" / "fixtures"


def write_lines(root: Path, source: str, records: list) -> None:
    folder = Path(root) / source
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{source}.jsonl").write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n", encoding="utf-8")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "raw"
        self.rejects = Path(self.tmp.name) / "rejects.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_sample_loads_into_three_subgraphs(self):
        write_sample(self.root, include_bad=False)
        writer = MemoryWriter()
        report = run("brandon-dev", self.root, writer, self.rejects)
        self.assertEqual(report.rejected, 0, report.render())
        self.assertEqual(report.problems, [])
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.node_counts["WorkItem"], 2)       # github #42, EMBER-39
        self.assertEqual(report.node_counts["Change"], 2)         # pull 43, one commit
        self.assertEqual(report.node_counts["Message"], 3)        # jira comment, two slack messages
        self.assertEqual(report.node_counts["Conversation"], 1)   # the reply joins its parent's thread
        self.assertEqual(report.node_counts["Container"], 3)      # repo, project, channel
        self.assertEqual({v["group_id"] for v in writer.nodes.values()},
                         {"brandon-dev_github", "brandon-dev_jira", "brandon-dev_slack"})
        self.assertEqual(report.skipped["github:issue_is_pull"], 1)
        self.assertEqual(report.skipped["slack:team"], 1)
        self.assertEqual(report.skipped["github:other"], 0)
        self.assertTrue(any("EMBER-45" in w or "not in the graph" in w for w in report.warnings), report.warnings)

    def test_rerun_is_idempotent(self):
        write_sample(self.root, include_bad=False)
        writer = MemoryWriter()
        first = run("brandon-dev", self.root, writer, self.rejects)
        before = (len(writer.nodes), len(writer.edges))
        second = run("brandon-dev", self.root, writer, self.rejects)
        self.assertEqual((len(writer.nodes), len(writer.edges)), before)
        self.assertEqual(first.node_counts, second.node_counts)

    def test_bad_records_are_rejected_with_reasons_and_exit_nonzero(self):
        write_sample(self.root, include_bad=True)
        report = run("brandon-dev", self.root, MemoryWriter(), self.rejects)
        self.assertEqual(report.rejected, 3)
        self.assertEqual(report.exit_code, 1)
        rejects = [json.loads(line) for line in self.rejects.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rejects), 3)
        self.assertTrue(all({"file", "line", "reason"} <= set(r) for r in rejects))
        self.assertIn("repository", "\n".join(r["reason"] for r in rejects))
        self.assertGreater(report.node_counts["WorkItem"], 0)   # good records still loaded

    def test_reset_removes_the_tenants_stale_nodes_and_keeps_other_tenants(self):
        write_sample(self.root, include_bad=False)
        writer = MemoryWriter()
        run("brandon-dev", self.root, writer, self.rejects)
        other = Path(self.tmp.name) / "other"
        write_sample(other, include_bad=False)
        run("someone-else", other, writer, self.rejects)

        def keys(group_prefix):
            return {k for k, v in writer.nodes.items() if v["group_id"].startswith(group_prefix)}

        def has_commit(group_prefix):
            return any(v.get("external_id", "").endswith("abc1234def")
                       for v in writer.nodes.values() if v["group_id"].startswith(group_prefix))

        self.assertTrue(has_commit("brandon-dev_"))
        other_nodes = {k: dict(v) for k, v in writer.nodes.items() if v["group_id"].startswith("someone-else_")}
        other_edges = {k for k, v in writer.edges.items() if v["group_id"].startswith("someone-else_")}

        trimmed = Path(self.tmp.name) / "trimmed"   # the same sample without the commit record
        write_sample(trimmed, include_bad=False)
        github = trimmed / "github" / "github.jsonl"
        kept = [line for line in github.read_text(encoding="utf-8").splitlines() if '"sha"' not in line]
        self.assertEqual(len(kept), 3)
        github.write_text("\n".join(kept) + "\n", encoding="utf-8")

        run("brandon-dev", trimmed, writer, self.rejects)                 # no reset: the stale commit stays
        self.assertTrue(has_commit("brandon-dev_"))
        run("brandon-dev", trimmed, writer, self.rejects, reset=True)
        self.assertFalse(has_commit("brandon-dev_"))                       # the stale node is gone
        self.assertTrue(keys("brandon-dev_"))                              # the rest was reloaded
        self.assertEqual({k: v for k, v in writer.nodes.items() if v["group_id"].startswith("someone-else_")},
                         other_nodes)                                      # the other tenant is untouched
        self.assertEqual({k for k, v in writer.edges.items() if v["group_id"].startswith("someone-else_")},
                         other_edges)

    def test_render_mentions_every_section(self):
        write_sample(self.root, include_bad=True)
        text = run("brandon-dev", self.root, MemoryWriter(), self.rejects).render()
        for part in ("records read", "mapped", "skipped", "rejected", "nodes written", "edges written", "schema check"):
            self.assertIn(part, text)

    def test_render_says_edges_written_means_merged(self):
        write_sample(self.root, include_bad=False)
        report = run("brandon-dev", self.root, MemoryWriter(), self.rejects)
        text = report.render()
        self.assertIn("merged", text)
        self.assertIn(f"{report.edges_sent - report.edges_written} dropped", text)

    # A5: a sparse update must not erase what a fuller record already set.
    def _issue_nodes(self, records):
        write_lines(self.root, "jira", records)
        writer = MemoryWriter()
        report = run("brandon-dev", self.root, writer, self.rejects)
        self.assertEqual(report.rejected, 0, report.render())
        return [v for v in writer.nodes.values() if v.get("external_id") == "EMBER-39"]

    def test_sparse_webhook_after_full_issue_keeps_description_created_and_embed_text(self):
        sparse = json.loads((FIXTURES / "jira.json").read_text(encoding="utf-8"))
        self.assertEqual(sparse["body"]["issue"]["key"], "EMBER-39")
        (node,) = self._issue_nodes([JIRA[0], sparse])
        self.assertIn("One envelope for every connector.", node["embed_text"])
        self.assertEqual(node["created_at"], "2026-09-20T12:00:00.000+0000")
        self.assertEqual(node["status"], "In Progress")

    def test_full_issue_after_sparse_webhook_also_ends_complete(self):
        sparse = json.loads((FIXTURES / "jira.json").read_text(encoding="utf-8"))
        (node,) = self._issue_nodes([sparse, JIRA[0]])
        self.assertIn("One envelope for every connector.", node["embed_text"])
        self.assertEqual(node["created_at"], "2026-09-20T12:00:00.000+0000")

    # A6: one bad line never aborts the load.
    def test_mapper_exceptions_become_rejects_and_the_run_completes(self):
        write_sample(self.root, include_bad=False)
        write_lines(self.root, "slack_bad", [
            {"type": "slack", "body": {"name": "no-id", "is_channel": True}},
            {"type": "slack", "body": {"type": "message", "channel": "C012AB3CD", "user": "U012ABCDEF",
                                       "text": "bad ts", "ts": "abc"}},
        ])
        writer = MemoryWriter()
        report = run("brandon-dev", self.root, writer, self.rejects)
        self.assertEqual(report.rejected, 2, report.render())
        self.assertEqual(report.exit_code, 1)
        rejects = [json.loads(line) for line in self.rejects.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rejects), 2)
        self.assertTrue(all(r["reason"] for r in rejects))
        self.assertGreater(report.node_counts["WorkItem"], 0)   # the good records still loaded
        self.assertEqual(report.node_counts["Message"], 3)

    def test_unexpected_exception_reason_names_the_type(self):
        write_sample(self.root, include_bad=False)
        with mock.patch("loader.run.map_record", side_effect=KeyError("id")):
            report = run("brandon-dev", self.root, MemoryWriter(), self.rejects)
        reasons = [json.loads(line)["reason"] for line in self.rejects.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(reasons and all(r.startswith("mapper error: KeyError:") for r in reasons), reasons)
        self.assertEqual(report.mapped, 0)

    def test_envelope_missing_type_or_body_is_rejected_not_raised(self):
        # The reader normally flags these; guard anyway in case one gets through.
        write_lines(self.root, "github", ["{}"])  # the folder must hold a *.jsonl; the reader is mocked
        lines = [Line("f.jsonl", 1, {"body": {}}, None), Line("f.jsonl", 2, {"type": "github"}, None)]
        with mock.patch("loader.run.read_envelopes", return_value=iter(lines)):
            report = run("brandon-dev", self.root, MemoryWriter(), self.rejects)
        self.assertEqual(report.rejected, 2)
        self.assertEqual(report.records, 2)

    # Input problems stop the run before anything is written.
    def test_missing_input_folder_is_an_input_error(self):
        writer = MemoryWriter()
        with self.assertRaisesRegex(InputError, "does not exist"):
            run("brandon-dev", self.root, writer, self.rejects)
        self.assertFalse(self.rejects.exists())

    def test_empty_input_folder_is_an_input_error(self):
        self.root.mkdir(parents=True)
        with self.assertRaisesRegex(InputError, r"no \*\.jsonl"):
            run("brandon-dev", self.root, MemoryWriter(), self.rejects)

    def test_folder_with_only_parts_files_is_an_input_error(self):
        parts = self.root / "slack" / "C1.jsonl.parts"
        parts.mkdir(parents=True)
        (parts / "0001.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(InputError, r"no \*\.jsonl"):
            run("brandon-dev", self.root, MemoryWriter(), self.rejects)

    def test_reset_with_nothing_mapped_leaves_existing_nodes_untouched(self):
        other = Path(self.tmp.name) / "loaded"
        write_sample(other, include_bad=False)
        writer = MemoryWriter()
        run("brandon-dev", other, writer, self.rejects)
        before = (dict(writer.nodes), dict(writer.edges))
        self.assertTrue(before[0])
        write_lines(self.root, "github", ["{not json"])  # a *.jsonl exists but maps to nothing
        with self.assertRaisesRegex(InputError, "--reset refused"):
            run("brandon-dev", self.root, writer, self.rejects, reset=True)
        self.assertEqual((writer.nodes, writer.edges), before)

    def test_reset_is_called_only_after_nodes_were_mapped(self):
        write_sample(self.root, include_bad=False)
        writer = MemoryWriter()
        with mock.patch.object(writer, "reset", wraps=writer.reset) as reset:
            run("brandon-dev", self.root, writer, self.rejects, reset=True)
        reset.assert_called_once()


    def test_rejects_file_survives_a_writer_failure(self):
        write_sample(self.root, include_bad=True)

        class Boom(MemoryWriter):
            def write_nodes(self, nodes):
                raise RuntimeError("neo4j went away")

        with self.assertRaises(RuntimeError):
            run("brandon-dev", self.root, Boom(), self.rejects)
        rejects = [json.loads(line) for line in self.rejects.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rejects), 3)

    def test_no_rejects_file_when_nothing_was_rejected(self):
        write_sample(self.root, include_bad=False)
        run("brandon-dev", self.root, MemoryWriter(), self.rejects)
        self.assertFalse(self.rejects.exists())

if __name__ == "__main__":
    unittest.main()
