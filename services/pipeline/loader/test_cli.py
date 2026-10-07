import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loader.__main__ import main
from loader.testdata import write_sample


def run_cli(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(args))
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def test_dry_run_on_clean_data_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            code, text = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                                 "--rejects", str(Path(tmp) / "r.jsonl"), "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("schema check: OK", text)

    def test_dry_run_on_bad_data_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=True)
            code, text = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                                 "--rejects", str(Path(tmp) / "r.jsonl"), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("rejected:       3", text)

    def test_missing_password_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                run_cli("--tenant", "brandon-dev", "--input", tmp, "--no-dotenv")
        self.assertIn("NEO4J_PASSWORD", str(raised.exception))

    def test_embed_without_a_key_stops_before_loading(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            write_sample(Path(tmp) / "raw", include_bad=False)
            with self.assertRaises(SystemExit) as raised:
                run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"), "--dry-run",
                        "--embed", "--profile", "openai-dev", "--no-dotenv",
                        "--rejects", str(Path(tmp) / "r.jsonl"))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["raw"])  # nothing loaded or written
        self.assertIn("OPENAI_API_KEY", str(raised.exception))

    def test_embed_key_error_fires_before_the_neo4j_writer_is_built(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True),                 mock.patch("loader.__main__._neo4j_writer", side_effect=AssertionError("Neo4j was touched")):
            write_sample(Path(tmp) / "raw", include_bad=False)
            with self.assertRaises(SystemExit) as raised:
                run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                        "--embed", "--profile", "openai-dev", "--no-dotenv")
        self.assertIn("OPENAI_API_KEY", str(raised.exception))

    def test_embed_flag_embeds_in_a_dry_run(self):
        env = {"OPENAI_API_KEY": "sk-test-not-real"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("loader.__main__.Client") as client_cls:
            client_cls.return_value.embed.side_effect = lambda texts, **kw: [[0.0, 1.0] for _ in texts]
            write_sample(Path(tmp) / "raw", include_bad=False)
            code, text = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                                 "--rejects", str(Path(tmp) / "r.jsonl"), "--dry-run",
                                 "--embed", "--profile", "openai-dev", "--no-dotenv")
        self.assertEqual(code, 0, text)
        self.assertIn("embedded:       7", text)
        self.assertNotIn("sk-test-not-real", text)

    def test_bad_tenant_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                run_cli("--tenant", "Bad_Tenant", "--input", tmp, "--dry-run")

    def test_stale_rejects_file_is_removed_by_a_clean_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=False)
            rejects = Path(tmp) / "r.jsonl"
            rejects.write_text('{"stale": true}\n', encoding="utf-8")
            code, text = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                                 "--rejects", str(rejects), "--dry-run")
            self.assertEqual(code, 0, text)
            self.assertFalse(rejects.exists())

    def test_bad_data_still_writes_the_rejects_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=True)
            rejects = Path(tmp) / "r.jsonl"
            rejects.write_text('{"stale": true}\n', encoding="utf-8")
            code, _ = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                              "--rejects", str(rejects), "--dry-run")
            self.assertEqual(code, 1)
            lines = rejects.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("stale", "".join(lines))

    def test_rejects_parent_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sample(Path(tmp) / "raw", include_bad=True)
            rejects = Path(tmp) / "out" / "nested" / "r.jsonl"
            code, _ = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "raw"),
                              "--rejects", str(rejects), "--dry-run")
            self.assertEqual(code, 1)
            self.assertTrue(rejects.exists())

    def test_missing_input_folder_exits_two_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run_cli("--tenant", "brandon-dev", "--input", str(Path(tmp) / "nope"),
                                  "--rejects", str(Path(tmp) / "r.jsonl"), "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err.getvalue())

    def test_empty_input_folder_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run_cli("--tenant", "brandon-dev", "--input", tmp, "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("no *.jsonl", err.getvalue())

    def test_reset_with_nothing_mapped_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.jsonl").write_text("{not json\n", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run_cli("--tenant", "brandon-dev", "--input", tmp,
                                  "--rejects", str(Path(tmp) / "out" / "r.jsonl"), "--dry-run", "--reset")
        self.assertEqual(code, 2)
        self.assertIn("--reset refused", err.getvalue())


if __name__ == "__main__":
    unittest.main()
