# services/pipeline/models/test_smoke.py
import contextlib
import io
import os
import unittest
from unittest import mock

from models.__main__ import main
from models.client import Client, ModelError
from models.config import DEFAULT_CONFIG, Profile, load_profile
from models.fake_server import FakeServer
from models.identity import identity_for
from models.smoke import smoke
from ontology.config import EXPECTED, EmbeddingIdentity


def profile(base_url, **overrides):
    values = dict(name="t", llm_base_url=base_url, llm_model="chat-x", llm_key_env="TEST_KEY",
                  embedding_base_url=base_url, embedding_model="emb-x", embedding_dimensions=4,
                  embedding_key_env="TEST_KEY")
    values.update(overrides)
    return Profile(**values)


class IdentityTests(unittest.TestCase):
    def test_identity_uses_the_profile(self):
        p = profile("http://x", embedding_revision="r1", query_template="{query}")
        self.assertEqual(identity_for(p), EmbeddingIdentity("emb-x", "r1", 4, "{query}"))

    def test_identity_model_overrides_the_served_name(self):
        self.assertEqual(identity_for(profile("http://x", identity_model="Org/Model")).model, "Org/Model")

    def test_local_spark_profile_matches_the_recorded_identity(self):
        spark = load_profile("local-spark", path=DEFAULT_CONFIG, env={})
        self.assertEqual(identity_for(spark), EXPECTED)


class SmokeTests(unittest.TestCase):
    def test_smoke_reports_both_calls(self):
        with FakeServer() as server:
            p = profile(server.base_url)
            lines = smoke(p, Client(p, {"TEST_KEY": "sk-test"}, sleep=lambda s: None))
        text = "\n".join(lines)
        self.assertIn("chat-x replied 'pong'", text)
        self.assertIn("emb-x returned 4 dimensions", text)

    def test_smoke_rejects_an_empty_chat_reply(self):
        class Stub:
            def chat(self, messages):
                return "  "

            def embed(self, texts):
                return [[0.0] * 4]

        with self.assertRaises(ModelError) as ctx:
            smoke(profile("http://x"), Stub())
        self.assertIn("chat-x returned an empty reply", str(ctx.exception))

    def test_cli_without_a_key_exits_one_with_the_variable_name(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"MODEL_PROFILE": "openai-dev"}, clear=True), \
                contextlib.redirect_stderr(err):
            code = main(["smoke", "--no-dotenv"])
        self.assertEqual(code, 1)
        self.assertIn("OPENAI_API_KEY", err.getvalue())


if __name__ == "__main__":
    unittest.main()
