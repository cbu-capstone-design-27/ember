import tempfile
import unittest
from pathlib import Path

from models.config import ConfigError, DEFAULT_CONFIG, load_dotenv, load_profile

TOML = """
default_profile = "a"

[profiles.a]
llm_base_url = "https://llm.example/v1"
llm_model = "chat-1"
llm_key_env = "A_KEY"
embedding_base_url = "https://emb.example/v1"
embedding_model = "emb-1"
embedding_dimensions = 8
embedding_key_env = "A_KEY"

[profiles.b]
llm_base_url = "https://b.example/v1"
llm_model = "chat-2"
llm_key_env = "B_KEY"
embedding_base_url = "https://b.example/v1"
embedding_model = "emb-2"
embedding_dimensions = 16
embedding_key_env = "B_KEY"
send_dimensions = false
"""


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "models.toml"
        self.path.write_text(TOML, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_profile_and_defaults(self):
        p = load_profile(path=self.path, env={})
        self.assertEqual((p.name, p.llm_model, p.embedding_dimensions), ("a", "chat-1", 8))
        self.assertTrue(p.send_dimensions)
        self.assertEqual(p.query_template, "{query}")

    def test_env_selects_profile_and_argument_wins(self):
        self.assertEqual(load_profile(path=self.path, env={"MODEL_PROFILE": "b"}).name, "b")
        self.assertEqual(load_profile("a", path=self.path, env={"MODEL_PROFILE": "b"}).name, "a")

    def test_fixture_profile_b_has_send_dimensions_false(self):
        b = load_profile("b", path=self.path, env={})
        self.assertFalse(b.send_dimensions)

    def test_unknown_profile_lists_the_choices(self):
        with self.assertRaisesRegex(ConfigError, "a, b"):
            load_profile("zzz", path=self.path, env={})

    def test_missing_field_is_named(self):
        self.path.write_text(TOML.replace('llm_model = "chat-1"\n', ""), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "llm_model"):
            load_profile("a", path=self.path, env={})

    def test_base_url_overrides_ignore_empty_values(self):
        env = {"LLM_BASE_URL": "http://x/v1", "EMBEDDING_BASE_URL": ""}
        p = load_profile("a", path=self.path, env=env)
        self.assertEqual(p.llm_base_url, "http://x/v1")
        self.assertEqual(p.embedding_base_url, "https://emb.example/v1")

    def test_key_comes_from_the_named_variable_and_errors_do_not_leak(self):
        p = load_profile("a", path=self.path, env={})
        self.assertEqual(p.llm_key({"A_KEY": " sk-1 "}), "sk-1")
        with self.assertRaises(ConfigError) as raised:
            p.embedding_key({"A_KEY": "   ", "OTHER_KEY": "sk-test-not-a-real-key"})
        self.assertIn("A_KEY", str(raised.exception))
        self.assertNotIn("sk-test-not-a-real-key", str(raised.exception))


class RealConfigTests(unittest.TestCase):
    def test_shipped_profiles_load(self):
        openai = load_profile("openai-dev", path=DEFAULT_CONFIG, env={})
        self.assertEqual(openai.embedding_dimensions, 1024)
        self.assertNotIn("<", openai.llm_model + openai.embedding_model)  # Task 1 filled in real ids

    def test_local_spark_profile_is_configured(self):
        spark = load_profile("local-spark", path=DEFAULT_CONFIG, env={})
        self.assertFalse(spark.send_dimensions)
        self.assertEqual(spark.identity_model, "Qwen/Qwen3-Embedding-0.6B")
        self.assertIn("{task}", spark.query_template)
        self.assertEqual(spark.embedding_dimensions, 1024)
        self.assertTrue(spark.embedding_revision)  # non-empty


class DotenvTests(unittest.TestCase):
    def test_reads_values_without_overriding_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\nA=1\nB="two words"\nC=\nbad line\nEXISTING=file\n', encoding="utf-8")
            env = {"EXISTING": "shell"}
            load_dotenv(path, env)
        self.assertEqual(env, {"EXISTING": "shell", "A": "1", "B": "two words", "C": ""})

    def test_missing_file_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.env"
            load_dotenv(path, {})


if __name__ == "__main__":
    unittest.main()
