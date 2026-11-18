"""Graphiti on the fork's PostgresDriver, a real Postgres, the fake models. Skipped unless GRAPHITI_TEST_DATABASE_URL
is set to a local Postgres with the repo's migrations (infra/k8s/apps/postgres/migrations):

    GRAPHITI_TEST_DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
        python -m unittest graphiti_test.test_postgres_integration -v

It empties tenant photon's harness graphs (graph.graphs scope graphiti-test) and leaves them behind.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graphiti_test import fake_models
from graphiti_test.report import compare, split_ranking

URL = os.environ.get("GRAPHITI_TEST_DATABASE_URL")


def arguments(out, **overrides):
    values = dict(tenant="photon", limit=20, profile="local-spark", fake=True, out=str(out), database_url=URL, reset=True,
                  concurrency=4, structured_mode="json_schema", no_resend_check=False, verbose=False)
    return SimpleNamespace(**{**values, **overrides})


def go(args):
    from graphiti_test.run import run

    return asyncio.run(run(args))


@unittest.skipUnless(URL, "GRAPHITI_TEST_DATABASE_URL not set")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name) / "first"
        cls.exit_code = go(arguments(cls.out))
        cls.reference = json.loads((cls.out / "reference.json").read_text(encoding="utf-8"))

    def test_the_run_is_clean_and_says_it_used_fakes(self):
        self.assertEqual(self.exit_code, 0)
        self.assertTrue(self.reference["fake_models"])
        self.assertIn("FAKE MODELS", (self.out / "summary.md").read_text(encoding="utf-8"))

    def test_the_migrations_indexes_are_online(self):
        compatibility = self.reference["measured"]["compatibility"]
        self.assertIsNone(compatibility["index_build_error"])
        self.assertEqual(compatibility["indexes_not_online"], 0)
        self.assertIn("edges_current", {i["name"] for i in compatibility["indexes"]})

    def test_each_source_lands_in_its_own_subgraph_on_declared_types(self):
        comparable = self.reference["comparable"]
        self.assertEqual(sorted(comparable["graph"]), ["photon_jira", "photon_slack"])
        self.assertEqual(comparable["isolation"]["crossing_edges"], 0)
        self.assertEqual(sum(g["episodes"] for g in comparable["graph"].values()), 20)
        for group in comparable["graph"].values():
            self.assertGreater(group["edges"], 0)
            self.assertNotIn("Entity (no declared type)", group["entities_by_type"])
            self.assertEqual(group["entities_missing_source_ref"], {})  # ADR 0002 criterion 5
            self.assertEqual(group["edges_off_the_ontology_map"], {})

    def test_cost_is_recorded_per_episode(self):
        episodes = self.reference["measured"]["episodes"]
        self.assertEqual(episodes["llm_calls"]["n"], 20)
        self.assertGreater(episodes["llm_calls"]["min"], 0)
        self.assertGreater(episodes["embedding_requests"]["min"], 0)
        self.assertEqual(episodes["structured_failures"], 0)
        self.assertNotIn("unnamed", episodes["by_prompt"])

    def test_every_reranker_runs_on_every_question(self):
        rows = self.reference["comparable"]["search"]
        self.assertEqual({r["reranker"] for r in rows},
                         {"rrf", "mmr", "cross_encoder", "node_distance", "episode_mentions"})
        self.assertEqual(len(rows), 9 * 5)
        self.assertEqual([r for r in rows if "error" in r and r["reranker"] != "mmr"], [])

    @unittest.expectedFailure
    def test_mmr_runs_on_every_question(self):
        """Known fork gap: its node upsert sets embedding = excluded.embedding, so a resave without a vector nulls
        it, and MMR gets None. Passes once the fork keeps the stored vector."""
        rows = self.reference["comparable"]["search"]
        self.assertEqual([r for r in rows if "error" in r and r["reranker"] == "mmr"], [])

    def test_the_old_fact_is_ended_and_still_true_as_of_then(self):
        temporal = self.reference["comparable"]["temporal"]
        self.assertTrue(temporal["old_fact_ended"])
        self.assertTrue(temporal["old_fact_seen_before"])
        self.assertTrue(temporal["old_fact_gone_after"])
        self.assertTrue(any("GPUI" in e["fact"] for e in temporal["invalidated"]))

    def test_resending_a_delivery_adds_only_an_episode(self):
        resend = self.reference["comparable"]["resend"]
        self.assertEqual((resend["entities_added"], resend["edges_added"], resend["episodes_added"]), (0, 0, 1))

    def test_a_rerun_adds_nothing_and_a_fresh_run_compares_equal(self):
        self.assertEqual(go(arguments(self.out, reset=False)), 0)
        rerun = json.loads((self.out / "reference.json").read_text(encoding="utf-8"))
        self.assertEqual(rerun["measured"]["ingest"]["attempted"], 0)
        self.assertEqual(rerun["measured"]["ingest"]["skipped"], 20)
        self.assertEqual(rerun["comparable"]["resend"], self.reference["comparable"]["resend"])
        self.assertEqual(rerun["comparable"]["graph"], self.reference["comparable"]["graph"])
        fresh_out = Path(self.tmp.name) / "fresh"
        self.assertEqual(go(arguments(fresh_out)), 0)
        fresh = json.loads((fresh_out / "reference.json").read_text(encoding="utf-8"))
        hard, _order = split_ranking(compare(self.reference, fresh))  # order moves on its own: see split_ranking
        # Known fork gap: on Postgres, search and temporal results move between identical runs (top-3 membership,
        # not just order), so only what was written is compared here.
        self.assertEqual([d for d in hard if not d.startswith(("/search", "/temporal"))][:10], [])


@unittest.skipUnless(URL, "GRAPHITI_TEST_DATABASE_URL not set")
class ModelMisbehaves(unittest.TestCase):
    def test_a_null_required_attribute_fails_the_episode_and_is_recorded(self):
        """ADR 0002's open risk, end to end: nothing is written, the run goes on, the reference says what happened."""
        fake = functools.partial(fake_models.FakeModels, null_attributes=True)
        with tempfile.TemporaryDirectory() as tmp, mock.patch("graphiti_test.run.FakeModels", fake):
            out = Path(tmp) / "nulls"
            with self.assertLogs("graphiti_test", "WARNING"):
                self.assertEqual(go(arguments(out, limit=4, no_resend_check=True)), 1)
            reference = json.loads((out / "reference.json").read_text(encoding="utf-8"))
        episodes = reference["measured"]["episodes"]
        failed = [e for e in episodes["failed"] if e["failure"] == "structured_output"]
        self.assertTrue(failed, episodes)
        self.assertEqual(failed[0]["error_type"], "ValidationError")
        self.assertIn("external_id: string_type", failed[0]["error"])
        self.assertTrue(any(f["kind"] == "schema_mismatch" for f in reference["measured"]["structured_failures"]))
        self.assertEqual(reference["comparable"]["failed_records"], sorted(e["name"] for e in episodes["failed"]))
        written = sum(group["episodes"] for group in reference["comparable"]["graph"].values())
        self.assertEqual(written, reference["measured"]["ingest"]["ok"])  # a failed episode leaves nothing behind


if __name__ == "__main__":
    unittest.main()
