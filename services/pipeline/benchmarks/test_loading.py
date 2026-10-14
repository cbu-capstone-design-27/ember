import copy
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.loading import Check, run_loading, score_loading
from loader.memory_writer import MemoryWriter
from loader.run import run
from loader.testdata import write_sample
from ontology import tenant_subgraphs

DATA = Path(__file__).parent / "data"
SPEC = json.loads((DATA / "loading_sample.json").read_text(encoding="utf-8"))


def load_sample(include_bad=False):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "raw"
    write_sample(root, include_bad=include_bad)
    writer = MemoryWriter()
    report = run("brandon-dev", root, writer, Path(tmp.name) / "r.jsonl")
    snap = writer.snapshot(tenant_subgraphs("brandon-dev"))
    tmp.cleanup()
    return report, snap


class LoadingSuiteTests(unittest.TestCase):
    def test_the_sample_load_passes_every_check(self):
        report, snap = load_sample()
        checks = run_loading(SPEC, report, snap)
        failed = [c for c in checks if not c.ok]
        self.assertEqual(failed, [], "\n".join(f"{c.group}: {c.name}: {c.detail}" for c in failed))
        scores = score_loading(checks)
        self.assertEqual({g: s["percent"] for g, s in scores.items()},
                         {"all": 100.0, "github": 100.0, "jira": 100.0, "slack": 100.0})

    def test_a_wrong_count_field_and_edge_are_each_reported(self):
        report, snap = load_sample()
        spec = copy.deepcopy(SPEC)
        spec["nodes"]["github"]["WorkItem"] = 5
        spec["fields"][0]["props"]["status"] = "closed"
        spec["edges"][0]["dst"] = "github:acme/widget#99"
        failed = {c.name: c for c in run_loading(spec, report, snap) if not c.ok}
        self.assertIn("nodes WorkItem", failed)
        self.assertIn("expected 5, found 1", failed["nodes WorkItem"].detail)
        self.assertIn("field github:acme/widget#42 status", failed)
        self.assertIn("'closed'", failed["field github:acme/widget#42 status"].detail)
        self.assertTrue(any(n.startswith("edge RESOLVES") for n in failed))
        scores = score_loading(run_loading(spec, report, snap))
        self.assertLess(scores["github"]["percent"], 100.0)
        self.assertEqual(scores["jira"]["percent"], 100.0)

    def test_a_missing_node_fails_its_field_check(self):
        report, snap = load_sample()
        spec = copy.deepcopy(SPEC)
        spec["fields"].append({"ref": "github:acme/widget#777", "label": "WorkItem", "props": {"status": "open"}})
        failed = [c for c in run_loading(spec, report, snap) if not c.ok]
        self.assertEqual([(c.group, c.detail) for c in failed], [("github", "no WorkItem with that ref")])

    def test_rejects_and_accounting_are_checked_globally(self):
        report, snap = load_sample(include_bad=True)
        checks = {c.name: c for c in run_loading(SPEC, report, snap)}
        self.assertFalse(checks["reject rate"].ok)
        self.assertTrue(checks["records accounted for"].ok)
        self.assertTrue(checks["schema check"].ok)

    def test_schema_problems_fail_the_schema_check(self):
        report, snap = load_sample()
        report.problems = ["missing constraint x"]
        checks = {c.name: c for c in run_loading(SPEC, report, snap)}
        self.assertFalse(checks["schema check"].ok)
        self.assertIn("missing constraint x", checks["schema check"].detail)

    def test_score_of_no_checks_is_empty(self):
        self.assertEqual(score_loading([]), {})
        self.assertEqual(score_loading([Check("all", "x", True)])["all"],
                         {"passed": 1, "total": 1, "percent": 100.0})


if __name__ == "__main__":
    unittest.main()
