# services/pipeline/benchmarks/test_main.py
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from benchmarks.__main__ import main, run_suites
from benchmarks.results import DEV_NOTE, build_meta, data_version, render_summary, save_result
from benchmarks.retrieval_suite import Doc
from benchmarks.testing import BAG_IDENTITY, BagClient, bag_of_words_embed
from loader.embed import embed_graph
from loader.memory_writer import MemoryWriter
from loader.run import run
from loader.testdata import write_sample
from models.config import Profile
from ontology import tenant_subgraphs

DATA = Path(__file__).parent / "data"
PROFILE = Profile(name="test", llm_base_url="http://x", llm_model="chat-test", llm_key_env="K",
                  embedding_base_url="http://x", embedding_model="emb-test", embedding_dimensions=64,
                  embedding_key_env="K")
KEY_ENV = {"OPENAI_API_KEY": "sk-x", "MODEL_PROFILE": "openai-dev"}


class ResultsTests(unittest.TestCase):
    def test_data_version_depends_on_text_and_not_on_order(self):
        a, b = Doc("x:1", "one", [0.0]), Doc("x:2", "two", [0.0])
        self.assertEqual(data_version([a, b]), data_version([b, a]))
        self.assertNotEqual(data_version([a, b]), data_version([a, Doc("x:2", "two!", [0.0])]))
        self.assertEqual(len(data_version([a])), 12)
        self.assertEqual(data_version([a]), data_version([a], extra=""))
        self.assertNotEqual(data_version([a]), data_version([a], extra="questions-v1"))

    def test_meta_names_the_models_and_carries_the_disclaimer(self):
        meta = build_meta("brandon-dev", "sample", PROFILE, [Doc("x:1", "one", [0.0])], today=date(2026, 10, 7))
        self.assertEqual((meta["date"], meta["tenant"], meta["llm_model"], meta["embedding_model"]),
                         ("2026-10-07", "brandon-dev", "chat-test", "emb-test"))
        self.assertEqual(meta["note"], DEV_NOTE)
        self.assertIn("not comparable", DEV_NOTE)

    def test_save_and_render(self):
        result = {"suite": "retrieval", "meta": {"date": "2026-10-07", "tenant": "t", "note": DEV_NOTE,
                                                 "embedding_model": "e", "llm_model": "l", "data_version": "abc",
                                                 "dataset": "d"},
                  "scores": {"bm25": {"recall@1": 0.5, "mrr": 0.6}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = save_result(result, Path(tmp))
            self.assertEqual(path.name, "2026-10-07-retrieval-t.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)
        text = render_summary(result)
        self.assertIn("bm25", text)
        self.assertIn("0.5", text)
        self.assertIn("not comparable", text)
        self.assertIn("llm l, embedding e", text)

    def test_profiles_on_the_same_day_do_not_overwrite_each_other(self):
        def result(suite, profile=None):
            meta = {"date": "2026-10-07", "tenant": "t", "note": DEV_NOTE}
            if profile:
                meta["profile"] = profile
            return {"suite": suite, "meta": meta, "scores": {}}

        with tempfile.TemporaryDirectory() as tmp:
            names = {save_result(result("retrieval", p), Path(tmp)).name for p in ("openai-dev", "local/spark")}
            names.add(save_result(result("loading"), Path(tmp)).name)
        self.assertEqual(names, {"2026-10-07-retrieval-t-openai-dev.json", "2026-10-07-retrieval-t-local-spark.json",
                                 "2026-10-07-loading-t.json"})


class RunSuitesTests(unittest.TestCase):
    def test_both_suites_on_the_sample_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "raw", Path(tmp) / "out"
            write_sample(root, include_bad=False)
            writer = MemoryWriter()
            report = run("brandon-dev", root, writer, Path(tmp) / "r.jsonl")
            embed_graph(writer, BagClient(), BAG_IDENTITY, tenant_subgraphs("brandon-dev"))
            results = run_suites(["loading", "retrieval"], tenant="brandon-dev", report=report, writer=writer,
                                 loading_spec=DATA / "loading_sample.json",
                                 questions_file=DATA / "questions_sample.json",
                                 embed_queries=bag_of_words_embed, profile=PROFILE, out_dir=out)
            self.assertEqual([r["suite"] for r in results], ["loading", "retrieval"])
            self.assertEqual(results[0]["scores"]["all"]["percent"], 100.0)
            self.assertEqual(results[1]["scores"]["hybrid"]["recall@10"], 1.0)
            self.assertEqual(results[1]["meta"]["embedding_model"], "emb-test")
            self.assertEqual(results[1]["meta"]["note"], DEV_NOTE)
            # File names are checked against the date recorded in the results, so a run across midnight cannot flake.
            day = results[0]["meta"]["date"]
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(names, [f"{day}-loading-brandon-dev.json", f"{day}-retrieval-brandon-dev-test.json"])
            self.assertEqual(results[1]["meta"]["date"], day)
            self.assertEqual((results[0]["meta"]["rejected"], results[0]["meta"]["problems"]), (0, 0))
            saved = json.loads((out / names[0]).read_text(encoding="utf-8"))
            self.assertEqual(len(results[0]["checks"]), len(saved["checks"]))


class CliTests(unittest.TestCase):
    def _run_cli(self, tmp, *extra, env=None, client=None, suite="loading", spec=None, questions=None):
        out, err = io.StringIO(), io.StringIO()
        argv = ["run", "--suite", suite, "--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                "--no-dotenv", "--out", str(Path(tmp) / "out"), "--rejects", str(Path(tmp) / "r.jsonl"),
                "--spec", str(spec or DATA / "loading_sample.json"),
                "--questions", str(questions or DATA / "questions_sample.json"), *extra]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env or {}, clear=True))
            if client is not None:
                stack.enter_context(mock.patch("benchmarks.__main__.Client", client))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_retrieval_without_a_key_exits_one_naming_the_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"MODEL_PROFILE": "openai-dev"}, clear=True), \
                    contextlib.redirect_stderr(err):
                code = main(["run", "--suite", "retrieval", "--tenant", "brandon-dev",
                             "--input", str(Path(tmp) / "raw"), "--no-dotenv",
                             "--rejects", str(Path(tmp) / "r.jsonl"),
                             "--questions", str(DATA / "questions_sample.json")])
        self.assertEqual(code, 1)
        self.assertIn("OPENAI_API_KEY", err.getvalue())

    def test_loading_suite_needs_no_key_and_prints_a_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out):
                code = main(["run", "--suite", "loading", "--tenant", "brandon-dev",
                             "--input", str(Path(tmp) / "raw"), "--no-dotenv",
                             "--spec", str(DATA / "loading_sample.json"), "--out", str(Path(tmp) / "out"),
                             "--rejects", str(Path(tmp) / "r.jsonl")])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("loading", out.getvalue())
        self.assertIn("100.0", out.getvalue())

    def test_docs_command_lists_refs_and_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["docs", "--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                             "--rejects", str(Path(tmp) / "r.jsonl")])
        self.assertEqual(code, 0)
        self.assertIn("github:acme/widget#42\tLogin redirect drops the query string", out.getvalue())

    def test_missing_input_folder_exits_two_and_keeps_the_previous_rejects_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rejects = Path(tmp) / "r.jsonl"
            rejects.write_text("previous run\n", encoding="utf-8")
            err = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(err):
                code = main(["run", "--suite", "loading", "--tenant", "brandon-dev",
                             "--input", str(Path(tmp) / "nope"), "--no-dotenv", "--rejects", str(rejects),
                             "--spec", str(DATA / "loading_sample.json"), "--out", str(Path(tmp) / "out")])
            self.assertEqual(code, 2)
            self.assertIn("input folder does not exist", err.getvalue())
            self.assertEqual(rejects.read_text(encoding="utf-8"), "previous run\n")

    def test_bad_spec_or_questions_exit_two_before_anything_is_loaded_or_sent(self):
        class ForbiddenClient:
            def __init__(self, profile):
                raise AssertionError("the model client must not be created")

        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            bad_json, bad_shape, no_answers = (Path(tmp) / n for n in ("a.json", "b.json", "c.json"))
            bad_json.write_text("{not json", encoding="utf-8")
            bad_shape.write_text('{"nodes": [1, 2]}', encoding="utf-8")
            no_answers.write_text('{"questions": [{"id": "q1", "question": "why?"}]}', encoding="utf-8")
            missing = Path(tmp) / "missing.json"
            cases = [("loading", {"spec": missing}, "cannot read the loading spec"),
                     ("loading", {"spec": bad_json}, "not valid JSON"),
                     ("loading", {"spec": bad_shape}, "malformed"),
                     ("retrieval", {"questions": missing}, "cannot read the questions"),
                     ("retrieval", {"questions": bad_json}, "not valid JSON"),
                     ("retrieval", {"questions": no_answers}, "at least one answer"),
                     ("all", {"spec": bad_json}, "not valid JSON")]
            with mock.patch("benchmarks.__main__.run_loader") as loader:
                for suite, files, expected in cases:
                    with self.subTest(suite=suite, files=files):
                        code, _, err = self._run_cli(tmp, suite=suite, client=ForbiddenClient, env=KEY_ENV, **files)
                        self.assertEqual(code, 2)
                        self.assertIn(expected, err)
                        self.assertNotIn("Traceback", err)
                loader.assert_not_called()

    def test_unknown_answer_refs_exit_two_before_any_embedding(self):
        calls = []

        class CountingClient:
            def __init__(self, profile):
                pass

            def embed(self, texts, **kwargs):
                calls.append(len(texts))
                return bag_of_words_embed(texts)

        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            questions = Path(tmp) / "q.json"
            questions.write_text(json.dumps({"questions": [
                {"id": "q1", "question": "login redirect", "answers": ["github:acme/widget#9999"]}]}),
                encoding="utf-8")
            code, _, err = self._run_cli(tmp, suite="retrieval", client=CountingClient, questions=questions,
                                         env=KEY_ENV)
        self.assertEqual(code, 2)
        self.assertIn("github:acme/widget#9999", err)
        self.assertEqual(calls, [])

    def test_docs_with_neo4j_only_reads_and_never_resets_or_loads(self):
        class ReadOnlyWriter:
            closed = False

            def embed_candidates(self, group_ids):
                return [{"label": "WorkItem", "uuid": "u", "group_id": group_ids[0], "key": "acme/widget#1",
                         "text": "Existing  node", "hash": None, "vector": None}]

            def close(self):
                self.closed = True

            def __getattr__(self, name):  # reset, write_nodes, ensure_schema, ...
                raise AssertionError(f"docs must not call {name}")

        writer = ReadOnlyWriter()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("benchmarks.__main__._writer", return_value=writer), \
                mock.patch("benchmarks.__main__.run_loader") as loader, contextlib.redirect_stdout(out):
            code = main(["docs", "--tenant", "brandon-dev", "--neo4j", "--no-dotenv"])
        self.assertEqual(code, 0)
        loader.assert_not_called()
        self.assertTrue(writer.closed)
        self.assertIn("github:acme/widget#1\tExisting node", out.getvalue())

    def test_run_with_neo4j_resets_the_tenant_through_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            with mock.patch("benchmarks.__main__._writer", return_value=MemoryWriter()), \
                    mock.patch("benchmarks.__main__.run_loader", wraps=run) as loader:
                code, _, err = self._run_cli(tmp, "--neo4j")
        self.assertEqual(code, 0, err)
        self.assertIs(loader.call_args.kwargs["reset"], True)

    def test_failed_loading_check_exits_one_after_saving_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            spec = json.loads((DATA / "loading_sample.json").read_text(encoding="utf-8"))
            spec["nodes"]["github"]["WorkItem"] = 99
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            code, out, err = self._run_cli(tmp, spec=spec_path)
            saved = list((Path(tmp) / "out").glob("*-loading-brandon-dev.json"))
        self.assertEqual(code, 1)
        self.assertEqual(len(saved), 1)
        self.assertIn("FAILED", out)
        self.assertIn("1 loading checks failed", err)

    def test_rejected_records_exit_one_and_are_recorded_in_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=True)
            spec = json.loads((DATA / "loading_sample.json").read_text(encoding="utf-8"))
            spec["max_reject_rate"] = 1.0  # tolerate rejects, so only the clean-load rule is under test
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            code, out, err = self._run_cli(tmp, spec=spec_path)
            (path,) = (Path(tmp) / "out").glob("*-loading-brandon-dev.json")
            meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        self.assertEqual(code, 1, out)
        self.assertGreater(meta["rejected"], 0)
        self.assertIn("records were rejected", err)
        self.assertIn("load was not clean", out)

    def test_retrieval_cli_with_a_fake_client_records_models_and_no_key(self):
        secret = "sk-test-secret-value"

        class FakeClient:
            def __init__(self, profile):
                self.profile = profile

            def embed(self, texts, **kwargs):
                return bag_of_words_embed(texts)

        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            out_dir = Path(tmp) / "out"
            code, out, err = self._run_cli(tmp, suite="retrieval", client=FakeClient,
                                           env={"OPENAI_API_KEY": secret, "MODEL_PROFILE": "openai-dev"})
            self.assertEqual(code, 0, out + err)
            (path,) = out_dir.glob("*-retrieval-brandon-dev-openai-dev.json")
            saved = path.read_text(encoding="utf-8")
            meta = json.loads(saved)["meta"]
            self.assertEqual(meta["embedding_model"], "text-embedding-3-small")
            self.assertEqual(meta["llm_model"], "gpt-5-nano")
            self.assertEqual(meta["note"], DEV_NOTE)
            self.assertEqual((meta["rejected"], meta["problems"]), (0, 0))
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, out + err)
            self.assertIn("not comparable", out)


if __name__ == "__main__":
    unittest.main()
