"""The harness without Graphiti or a database: selection, episodes, ledger, resume, counters, reference, fake models.

From services/pipeline: python -m unittest discover -s graphiti_test -t . -p "test_*.py" -v
No key, no network beyond 127.0.0.1. test_graphiti.py covers the parts that import graphiti_core.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from ontology import subgraph_id

from graphiti_test import fake_models
from graphiti_test.__main__ import main
from graphiti_test.data import load_rows, questions, record_of, render, select_rows, temporal_rows
from graphiti_test.ingest import Ledger, classify, describe_error, ingest
from graphiti_test.metrics import Counters, CountingTransport, delta
from graphiti_test.report import FAKE_WARNING, FORMAT, build_reference, compare, render_summary, top_k, write_reference


class Stub:
    """Stands in for Graphiti: records add_episode calls, fails on demand, costs one counted call each."""

    fail_with: Exception = ConnectionError("boom")

    def __init__(self, counters=None, fail=()):
        self.calls, self.fail, self.counters = [], set(fail), counters

    async def add_episode(self, **kwargs):
        self.calls.append(kwargs)
        if self.counters:
            self.counters.add_call("extract_nodes.extract_message")
            self.counters.add_request("llm", 200, {"prompt_tokens": 10, "completion_tokens": 2}, 0,
                                      "extract_nodes.extract_message")
        if kwargs["name"] in self.fail:
            raise self.fail_with
        return SimpleNamespace(nodes=[1, 2], edges=[1])


def run_ingest(stub, rows, ledger, counters=None, existing=frozenset(), secrets=()):
    return asyncio.run(ingest(stub, rows, "photon", ledger, counters or Counters(), lambda name: name,
                              set(existing), secrets))


class Rows(unittest.TestCase):
    def test_every_record_is_read_in_the_sources_order(self):
        rows = load_rows()
        self.assertEqual(len(rows), 112 + 29 + 637)  # the fixture README's totals
        self.assertEqual([r.order for r in rows], sorted(r.order for r in rows))
        self.assertEqual(len({r.name for r in rows}), len(rows))
        self.assertTrue(all(r.occurred_at.tzinfo for r in rows))

    def test_episode_names_give_back_the_object_ids_questions_expect(self):
        rows = load_rows()
        ids = {record_of(r.name) for r in rows}
        for question in questions(rows):
            self.assertTrue(question["expected"], question["id"])
            self.assertLessEqual(set(question["expected"]), ids, question["id"])
        self.assertEqual(sum(q["id"].startswith("ember-78/") for q in questions(rows)), 5)
        self.assertEqual(record_of("slack/message/C08PHOTONDV/1789392360.000100@1789392360.000100"),
                         "C08PHOTONDV/1789392360.000100")

    def test_a_sample_keeps_the_expected_records_and_the_change_of_fact(self):
        rows = load_rows()
        sample = select_rows(rows, 40)
        self.assertEqual(len(sample), 40)
        self.assertEqual([r.order for r in sample], sorted(r.order for r in sample))
        held = {r.object_id for r in sample}
        for question in questions(rows):
            self.assertLessEqual(set(question["expected"]), held, question["id"])
        self.assertLessEqual({r.object_id for r in temporal_rows(rows)}, held)
        self.assertEqual(len(temporal_rows(rows)), 2)
        self.assertEqual(len(select_rows(rows, 3)), 3)
        self.assertEqual(len(select_rows(rows, None)), len(rows))

    def test_episodes_carry_the_text_and_the_ids_attributes_need(self):
        rows = load_rows()
        issue = next(r for r in rows if r.kind == "issue" and r.record["issue"]["key"] == "PHO-19")
        body = json.loads(render(issue)["episode_body"])
        self.assertEqual((body["source"], body["kind"], body["external_id"]), ("jira", "issue", "PHO-19"))
        self.assertEqual((body["issue_type"], body["status"]), ("Task", "To Do"))
        self.assertEqual(render(issue)["source"], "json")
        message = render(temporal_rows(rows)[1])
        self.assertEqual(message["source"], "message")
        self.assertTrue(message["episode_body"].startswith("Ryan Stoffel: "))
        self.assertIn("GPUI did not pass", message["episode_body"])
        self.assertIn("#photon-dev", message["source_description"])
        comment = render(next(r for r in rows if r.kind == "comment"))
        self.assertIn("on Jira issue PHO-", comment["episode_body"])
        self.assertTrue(all(render(r)["episode_body"].strip() for r in rows))


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "episodes.jsonl"
        self.rows = list(load_rows()[:6])

    def test_rows_go_in_order_with_the_subgraph_and_reference_time(self):
        stub = Stub()
        summary = run_ingest(stub, self.rows, Ledger(self.path))
        self.assertEqual(summary["ok"], 6)
        self.assertEqual([c["name"] for c in stub.calls], [r.name for r in self.rows])
        for call, row in zip(stub.calls, self.rows):
            self.assertEqual(call["group_id"], subgraph_id("photon", row.source))
            self.assertEqual(call["reference_time"], row.occurred_at)
            self.assertIn("WorkItem", call["entity_types"])
            self.assertIn(("Person", "WorkItem"), call["edge_type_map"])
            self.assertIn("AUTHORED", call["edge_types"])

    def test_resume_skips_what_is_done_and_retries_what_failed(self):
        stub = Stub(fail={self.rows[2].name})
        first = run_ingest(stub, self.rows, Ledger(self.path))
        self.assertEqual((first["ok"], first["failed"]), (5, 1))
        again = Stub()
        second = run_ingest(again, self.rows, Ledger(self.path))
        self.assertEqual([c["name"] for c in again.calls], [self.rows[2].name])
        self.assertEqual((second["ok"], second["skipped"]), (1, 5))
        third = Stub()
        self.assertEqual(run_ingest(third, self.rows, Ledger(self.path))["skipped"], 6)
        self.assertEqual(third.calls, [])

    def test_an_episode_in_the_graph_without_a_ledger_line_is_not_added_twice(self):
        stub = Stub()
        summary = run_ingest(stub, self.rows, Ledger(self.path), existing={self.rows[0].name})
        self.assertNotIn(self.rows[0].name, [c["name"] for c in stub.calls])
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(Ledger(self.path).entries[self.rows[0].name]["status"], "already_in_graph")

    def test_a_torn_last_line_only_repeats_that_episode(self):
        run_ingest(Stub(), self.rows[:2], Ledger(self.path))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "half a li')
        self.assertEqual(set(Ledger(self.path).entries), {r.name for r in self.rows[:2]})

    def test_growing_the_limit_on_a_graph_warns_about_order(self):
        rows = [r for r in self.rows if r.source == self.rows[0].source][:3]
        run_ingest(Stub(), [rows[-1]], Ledger(self.path))
        with self.assertLogs("graphiti_test", "WARNING"):
            summary = run_ingest(Stub(), rows, Ledger(self.path))
        self.assertGreater(summary["out_of_order"], 0)

    def test_failures_stop_a_run_that_cannot_work_and_are_recorded_by_class(self):
        stub = Stub(fail={r.name for r in self.rows})
        with self.assertLogs("graphiti_test", "WARNING"):
            summary = run_ingest(stub, self.rows, Ledger(self.path))
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(summary["failed"], 5)
        entry = Ledger(self.path).entries[self.rows[0].name]
        self.assertEqual((entry["status"], entry["failure"], entry["error_type"]),
                         ("failed", "transient", "ConnectionError"))

    def test_per_episode_counters_are_that_episodes_share(self):
        counters = Counters()
        run_ingest(Stub(counters), self.rows[:3], Ledger(self.path), counters)
        for row in self.rows[:3]:
            entry = Ledger(self.path).entries[row.name]
            self.assertEqual(entry["counters"]["roles"]["llm"]["requests"], 1)
            self.assertEqual(entry["counters"]["prompts"]["extract_nodes.extract_message"]["calls"], 1)
            self.assertGreaterEqual(entry["wall_s"], 0)

    def test_a_key_in_an_error_never_reaches_the_ledger(self):
        stub = Stub(fail={self.rows[0].name})
        stub.fail_with = RuntimeError("401 bad key sk-very-secret")
        run_ingest(stub, self.rows[:1], Ledger(self.path), secrets=("sk-very-secret",))
        self.assertNotIn("sk-very-secret", self.path.read_text(encoding="utf-8"))
        self.assertIn("***", Ledger(self.path).entries[self.rows[0].name]["error"])

    def test_failures_are_classified(self):
        def named(name):
            return type(name, (Exception,), {})()

        self.assertEqual(classify(named("RateLimitError")), "throttled")
        self.assertEqual(classify(json.JSONDecodeError("x", "y", 0)), "structured_output")
        self.assertEqual(classify(named("ValidationError")), "structured_output")
        self.assertEqual(classify(named("APITimeoutError")), "transient")
        self.assertEqual(classify(ValueError("x")), "other")

    def test_a_validation_error_gives_fields_not_the_models_text(self):
        from pydantic import BaseModel, ValidationError

        class Model(BaseModel):
            external_id: str

        with self.assertRaises(ValidationError) as caught:
            Model(external_id=None)
        text = describe_error(caught.exception)
        self.assertIn("external_id: string_type", text)
        self.assertNotIn("input_value", text)


class CountingTransportTests(unittest.TestCase):
    def _send(self, role, body, status=200):
        counters = Counters()
        inner = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
        request = httpx.Request("POST", "http://x/v1/embeddings", json={"input": ["a", "b", "c"]})
        asyncio.run(CountingTransport(inner, counters, role).handle_async_request(request))
        return counters

    def test_chat_tokens_are_counted(self):
        counters = self._send("llm", {"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        self.assertEqual(counters.roles["llm"]["requests"], 1)
        self.assertEqual((counters.roles["llm"]["input_tokens"], counters.roles["llm"]["output_tokens"]), (7, 3))

    def test_responses_api_usage_names_are_counted(self):
        counters = self._send("llm", {"usage": {"input_tokens": 9, "output_tokens": 4}})
        self.assertEqual((counters.roles["llm"]["input_tokens"], counters.roles["llm"]["output_tokens"]), (9, 4))

    def test_embedding_inputs_and_http_errors_are_counted(self):
        counters = self._send("embedding", {"error": {}}, status=500)
        self.assertEqual(counters.roles["embedding"]["inputs"], 3)
        self.assertEqual(counters.roles["embedding"]["http_errors"], 1)

    def test_a_snapshot_delta_is_only_what_happened_between(self):
        counters = Counters()
        counters.add_request("llm", 200, {"prompt_tokens": 1}, 0, "a")
        before = counters.snapshot()
        counters.add_request("llm", 200, {"prompt_tokens": 2}, 0, "b")
        counters.add_structured_failure("b", "invalid_json")
        change = delta(counters.snapshot(), before)
        self.assertEqual(change["roles"]["llm"]["input_tokens"], 2)
        self.assertEqual(list(change["prompts"]), ["b"])
        self.assertEqual(change["structured_failures"], 1)


def reference(**overrides):
    entries = {
        "a": {"status": "ok", "wall_s": 1.0, "counters": {
            "roles": {"llm": {"requests": 3, "input_tokens": 30, "output_tokens": 6},
                      "embedding": {"requests": 1, "inputs": 2}},
            "prompts": {"p": {"calls": 3, "requests": 3, "input_tokens": 30, "output_tokens": 6}},
            "structured_failures": 0}},
        "b": {"status": "failed", "failure": "structured_output", "error_type": "ValidationError", "error": "x",
              "wall_s": 2.0, "counters": {"roles": {}, "prompts": {}, "structured_failures": 1}},
    }
    arguments = dict(
        fake=True,
        engine={"graphiti_core": "0.31.2",
                "server": {"name": "Neo4j Kernel", "version": "5.26", "edition": "community"}},
        models={"profile": "local-spark"},
        run={"tenant": "photon", "limit": 2, "selected": ["b", "a"]},
        ingest={"ok": 1, "failed": 1, "skipped": 0, "out_of_order": 0, "stopped_early": False},
        ledger_entries=entries,
        graph={"photon_jira": {"episodes": 1, "entities": 2, "edges": 1, "edges_off_the_ontology_map": {},
                               "entities_missing_source_ref": {}}},
        isolation={"crossing_edges": 0},
        search=[{"id": "q", "reranker": "rrf", "in_top3": True},
                {"id": "q", "reranker": "cross_encoder", "error": {"type": "ValueError"}},
                {"id": "q", "reranker": "node_distance", "skipped": "no node"}],
        search_measures=[{"id": "q", "reranker": "rrf", "latency_ms": 5}],
        temporal={"query": "q", "invalidated": [{"fact": "old", "valid_at": "t0", "invalid_at": "t1"}],
                  "old_fact_seen_before": True, "old_fact_ended": True, "old_fact_gone_after": True},
        resend={"entities_added": 0, "edges_added": 0, "episodes_added": 1},
        compatibility={"indexes_online": 3, "indexes_not_online": 0, "index_build_error": None, "notifications": {}},
    )
    return build_reference(**{**arguments, **overrides})


class Reference(unittest.TestCase):
    def test_top_3_looks_in_each_scope(self):
        ranked = {"edges": ["x", "y", "z", "goal"], "nodes": ["goal"], "episodes": []}
        self.assertEqual(top_k(ranked, ["goal"]),
                         {"in_top3": True, "in_top3_by_scope": {"edges": False, "nodes": True, "episodes": False}})
        self.assertFalse(top_k({"edges": ["x", "y", "z", "goal"]}, ["goal"])["in_top3"])

    def test_the_shape_is_the_one_the_fork_will_be_compared_on(self):
        built = reference()
        self.assertEqual(built["format"], FORMAT)
        self.assertEqual(set(built["comparable"]), {"tenant", "selected_records", "failed_records", "graph",
                                                    "isolation", "search", "temporal", "resend"})
        self.assertEqual(built["comparable"]["failed_records"], ["b"])
        self.assertEqual(built["comparable"]["selected_records"], ["a", "b"])
        self.assertEqual(built["measured"]["episodes"]["llm_calls"]["total"], 3)
        self.assertEqual(built["measured"]["episodes"]["structured_failures"], 1)
        self.assertEqual(built["measured"]["episodes"]["episode_failures_by_class"], {"structured_output": 1})

    def test_measurements_never_make_a_difference(self):
        other = reference(search_measures=[{"latency_ms": 999}], engine={"graphiti_core": "fork", "server": {}},
                          compatibility={"indexes_online": 0})
        self.assertEqual(compare(reference(), other), [])

    def test_a_changed_result_does(self):
        other = reference(search=[{"id": "q", "reranker": "rrf", "in_top3": False}])
        found = compare(reference(), other)
        self.assertTrue(any("in_top3" in line for line in found), found)
        self.assertTrue(any("3 items != 1 items" in line for line in found), found)

    def test_different_formats_are_refused(self):
        other = reference()
        other["format"] = "something-else"
        with self.assertRaises(ValueError):
            compare(reference(), other)

    def test_a_fake_run_says_so_in_both_files_and_a_real_one_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = write_reference(reference(), Path(tmp))
            self.assertTrue(json.loads(json_path.read_text(encoding="utf-8"))["fake_models"])
            self.assertIn(FAKE_WARNING, json_path.read_text(encoding="utf-8"))
            self.assertIn(FAKE_WARNING, md_path.read_text(encoding="utf-8"))
        self.assertNotIn("FAKE", render_summary(reference(fake=False)))

    def test_the_summary_shows_errors_and_skips(self):
        summary = render_summary(reference())
        self.assertIn("error (ValueError)", summary)
        self.assertIn("skipped", summary)
        self.assertIn("yes (5 ms)", summary)

    def test_the_diff_command_exits_by_whether_they_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one.json", Path(tmp) / "two.json"
            one.write_text(json.dumps(reference()), encoding="utf-8")
            two.write_text(json.dumps(reference(isolation={"crossing_edges": 1})), encoding="utf-8")
            with mock.patch("sys.stdout"):
                self.assertEqual(main(["diff", str(one), str(one)]), 0)
                self.assertEqual(main(["diff", str(one), str(two)]), 1)


def chat(server, name, prompt, schema=None):
    payload = {"model": "m", "messages": [{"role": "user", "content": prompt}],
               "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema or {}}}}
    return json.loads(server.chat(payload)["choices"][0]["message"]["content"])


class FakeModelTests(unittest.TestCase):
    def setUp(self):
        self.server = fake_models.FakeModels(16)
        self.addCleanup(self.server._server.server_close)

    def test_embeddings_are_deterministic_unit_vectors_where_shared_words_score_higher(self):
        one = fake_models.embedding("photon release stack", 256)
        self.assertEqual(one, fake_models.embedding("photon release stack", 256))
        self.assertEqual(len(one), 256)
        self.assertAlmostEqual(sum(v * v for v in one), 1.0)

        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b))

        near = cosine(one, fake_models.embedding("photon release notes", 256))
        far = cosine(one, fake_models.embedding("clipboard arrow keys", 256))
        self.assertGreater(near, far)

    def test_embeddings_come_back_in_order_and_at_the_asked_size(self):
        rows = self.server.embeddings({"input": ["a", "b", "c"], "model": "m"})["data"]
        self.assertEqual([r["index"] for r in rows], [0, 1, 2])
        self.assertEqual(len(rows[0]["embedding"]), 16)

    def test_entities_are_found_in_the_current_message_only_with_the_prompts_type_ids(self):
        prompt = ("{'entity_type_id': 0, 'entity_type_name': 'Entity'}, "
                  "{'entity_type_id': 4, 'entity_type_name': 'WorkItem'}, "
                  "{'entity_type_id': 1, 'entity_type_name': 'Person'}"
                  "<PREVIOUS MESSAGES>Daniel Reyes on PHO-1</PREVIOUS MESSAGES>"
                  "<CURRENT MESSAGE>Ryan Stoffel: filed PHO-9</CURRENT MESSAGE>")
        found = chat(self.server, "ExtractedEntities", prompt)["extracted_entities"]
        self.assertEqual([(e["name"], e["entity_type_id"]) for e in found], [("Ryan Stoffel", 1), ("PHO-9", 4)])

    def test_the_change_of_fact_contradicts_the_old_one_in_the_prompts_repr_format(self):
        old, new = fake_models.SCRIPTED_FACTS[0][1], fake_models.SCRIPTED_FACTS[1][1]
        prompt = (f"<EXISTING FACTS>\n[{{'idx': 0, 'fact': '{old}'}}]\n</EXISTING FACTS>"
                  "<FACT INVALIDATION CANDIDATES>\n[{'idx': 1, 'fact': 'unrelated'}]\n</FACT INVALIDATION CANDIDATES>"
                  f"<NEW FACT>\n{new}\n</NEW FACT>")
        self.assertEqual(chat(self.server, "EdgeDuplicate", prompt), {"duplicate_facts": [], "contradicted_facts": [0]})
        same = prompt.replace(new, old)
        self.assertEqual(chat(self.server, "EdgeDuplicate", same), {"duplicate_facts": [0], "contradicted_facts": []})

    def test_any_other_schema_gets_its_required_fields(self):
        schema = {"required": ["source", "kind"],
                  "properties": {"source": {"type": "string"}, "kind": {"type": "string"},
                                 "status": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
        self.assertEqual(chat(self.server, "WorkItem", "x", schema),
                         {"source": "fake-source", "kind": "fake-kind", "status": None})
        self.server.null_attributes = True
        self.assertEqual(chat(self.server, "WorkItem", "x", schema), {"source": None, "kind": None, "status": None})

    def test_bad_json_mode_returns_text_that_does_not_parse(self):
        self.server.bad_json = True
        payload = {"messages": [{"role": "user", "content": "x"}],
                   "response_format": {"json_schema": {"name": "ExtractedEntities", "schema": {}}}}
        with self.assertRaises(ValueError):
            json.loads(self.server.chat(payload)["choices"][0]["message"]["content"])

    def test_the_reranker_call_gets_a_logprob_that_follows_word_overlap(self):
        def logprob(passage):
            prompt = f"<PASSAGE>{passage}</PASSAGE><QUERY>release stack</QUERY>"
            reply = self.server.chat({"logprobs": True, "messages": [{"role": "user", "content": prompt}]})
            return reply["choices"][0]["logprobs"]["content"][0]["top_logprobs"][0]["logprob"]

        self.assertGreater(logprob("the release stack"), logprob("clipboard history"))

    def test_the_http_server_wants_the_fake_key_and_reports_usage(self):
        server = fake_models.FakeModels(16)
        with server:
            url = server.base_url + "/embeddings"
            self.assertEqual(httpx.post(url, json={"input": ["a"]}).status_code, 401)
            ok = httpx.post(url, json={"input": ["a b"]}, headers={"Authorization": f"Bearer {fake_models.KEY}"})
            self.assertEqual(ok.json()["usage"]["prompt_tokens"], 2)


class Boundaries(unittest.TestCase):
    def test_no_cluster_neo4j_and_no_missing_password(self):
        from graphiti_test.run import SetupError, neo4j_settings

        for uri in ("bolt://hv-worker-1:7687", "bolt://neo4j.tail1234.ts.net:7687"):
            with self.assertRaises(SetupError):
                neo4j_settings(SimpleNamespace(neo4j_uri=uri), {"NEO4J_PASSWORD": "x"})
        with self.assertRaises(SetupError):
            neo4j_settings(SimpleNamespace(neo4j_uri="bolt://localhost:7687"), {})
        settings = neo4j_settings(SimpleNamespace(neo4j_uri=None), {"NEO4J_PASSWORD": "x"})
        self.assertEqual(settings[0], "bolt://localhost:7687")

    def test_only_the_made_up_tenant_is_accepted(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            main(["run", "--tenant", "ember-development", "--fake", "--out", "x"])


if __name__ == "__main__":
    unittest.main()
