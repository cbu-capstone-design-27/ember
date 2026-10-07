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


class ResultsTests(unittest.TestCase):
    def test_data_version_depends_on_text_and_not_on_order(self):
        a, b = Doc("x:1", "one", [0.0]), Doc("x:2", "two", [0.0])
        self.assertEqual(data_version([a, b]), data_version([b, a]))
        self.assertNotEqual(data_version([a, b]), data_version([a, Doc("x:2", "two!", [0.0])]))
        self.assertEqual(len(data_version([a])), 12)

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
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             [f"{date.today()}-loading-brandon-dev.json", f"{date.today()}-retrieval-brandon-dev.json"])
            self.assertEqual(len(results[0]["checks"]), len(json.loads((out / f"{date.today()}-loading-brandon-dev.json")
                                                                       .read_text(encoding="utf-8"))["checks"]))


class CliTests(unittest.TestCase):
    def test_retrieval_without_a_key_exits_one_naming_the_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"MODEL_PROFILE": "openai-dev"}, clear=True), \
                    contextlib.redirect_stderr(err):
                code = main(["run", "--suite", "retrieval", "--tenant", "brandon-dev",
                             "--input", str(Path(tmp) / "raw"), "--no-dotenv",
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

    def test_missing_input_folder_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(err):
                code = main(["run", "--suite", "loading", "--tenant", "brandon-dev",
                             "--input", str(Path(tmp) / "nope"), "--no-dotenv",
                             "--spec", str(DATA / "loading_sample.json"), "--out", str(Path(tmp) / "out")])
        self.assertEqual(code, 2)
        self.assertIn("input folder does not exist", err.getvalue())

    def test_retrieval_cli_with_a_fake_client_records_models_and_no_key(self):
        secret = "sk-test-secret-value"

        class FakeClient:
            def __init__(self, profile):
                self.profile = profile

            def embed(self, texts, **kwargs):
                return bag_of_words_embed(texts)

        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            out, out_dir = io.StringIO(), Path(tmp) / "out"
            env = {"OPENAI_API_KEY": secret, "MODEL_PROFILE": "openai-dev"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("benchmarks.__main__.Client", FakeClient), \
                    contextlib.redirect_stdout(out):
                code = main(["run", "--suite", "retrieval", "--tenant", "brandon-dev",
                             "--input", str(Path(tmp) / "raw"), "--no-dotenv", "--out", str(out_dir),
                             "--questions", str(DATA / "questions_sample.json"),
                             "--rejects", str(Path(tmp) / "r.jsonl")])
            self.assertEqual(code, 0, out.getvalue())
            saved = (out_dir / f"{date.today()}-retrieval-brandon-dev.json").read_text(encoding="utf-8")
            meta = json.loads(saved)["meta"]
            self.assertEqual(meta["embedding_model"], "text-embedding-3-small")
            self.assertEqual(meta["llm_model"], "gpt-5-nano")
            self.assertEqual(meta["note"], DEV_NOTE)
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, out.getvalue())
            self.assertIn("not comparable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
