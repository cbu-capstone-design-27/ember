"""Native Vertex wire format, task selection, accounting and client selection without cloud calls."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from google import genai
from google.auth.credentials import AnonymousCredentials
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

from models.config import ConfigError
from graphiti_test.metrics import Counters
from graphiti_test.run import SetupError, _query_embedder, model_identity
from graphiti_test.vertex import VertexSettings, build_vertex_clients, close_vertex_clients


ENV = {"GOOGLE_CLOUD_PROJECT": "test-project", "GOOGLE_CLOUD_LOCATION": "us-central1",
       "VERTEX_LLM_MODEL": "chosen-chat", "VERTEX_EMBEDDING_MODEL": "gemini-embedding-001",
       "VERTEX_EMBEDDING_DIMENSIONS": "3"}


class Answer(BaseModel):
    answer: str


class VertexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = VertexSettings.from_env(ENV)
        self.counters = Counters()
        self.requests = []
        self.vector = [3.0, 4.0, 0.0]
        self.status = 200
        original = genai.Client

        def client(**kwargs):
            self.assertTrue(kwargs["vertexai"])
            self.assertEqual(kwargs["project"], "test-project")
            self.assertNotIn("api_key", kwargs)
            credentials = AnonymousCredentials()
            credentials.token = "offline-test-token"
            kwargs["credentials"] = credentials
            # Keep the production counting transport, replacing only its network boundary.
            kwargs["http_options"].httpx_async_client._transport.inner = httpx.MockTransport(self.handle)
            return original(**kwargs)

        with mock.patch("graphiti_test.vertex.genai.Client", side_effect=client):
            self.clients = build_vertex_clients(self.settings, self.counters)

    async def asyncTearDown(self):
        await close_vertex_clients(SimpleNamespace(**self.clients))

    def handle(self, request):
        payload = json.loads(request.content)
        self.requests.append((request, payload))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"code": self.status, "message": "unavailable"}})
        if request.url.path.endswith(":predict"):
            return httpx.Response(200, json={"predictions": [{"embeddings": {
                "values": self.vector, "statistics": {"token_count": 7}}}]})
        text = '{"answer":"ok"}' if payload.get("generationConfig", {}).get("responseSchema") else "90"
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                                        "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3, "thoughtsTokenCount": 2}})

    async def test_native_documents_queries_and_batch_have_tasks_dimensions_and_counts(self):
        embedder = self.clients["embedder"]
        self.assertEqual(await embedder.create(["document"]), [0.6, 0.8, 0.0])
        query = await _query_embedder(SimpleNamespace(embedder=embedder), self.settings.profile)
        self.assertEqual(await query("question"), [0.6, 0.8, 0.0])
        await query("question")  # cached across rerankers
        self.assertEqual(await embedder.create_batch(["one", "two"]), [[0.6, 0.8, 0.0]] * 2)
        self.assertEqual(await embedder.create_batch([]), [])
        payloads = [p for _, p in self.requests]
        self.assertEqual([p["instances"][0]["task_type"] for p in payloads],
                         ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT", "RETRIEVAL_DOCUMENT"])
        self.assertTrue(all(p["parameters"]["outputDimensionality"] == 3 for p in payloads))
        self.assertTrue(all(r.url.host == "us-central1-aiplatform.googleapis.com" for r, _ in self.requests))
        usage = self.counters.roles["embedding"]
        self.assertEqual((usage["requests"], usage["inputs"], usage["input_tokens"]), (4, 4, 28))

    async def test_invalid_vectors_and_ambiguous_inputs_are_rejected(self):
        for vector in ([1.0], [], [0.0, 0.0, 0.0]):
            self.vector = vector
            with self.assertRaises(ValueError):
                await self.clients["embedder"].create("text")
        for value in (["one", "two"], [1, 2], []):
            with self.assertRaises(ValueError):
                await self.clients["embedder"].create(value)

    async def test_gemini_structured_extraction_and_reranker_count_separately(self):
        result = await self.clients["llm_client"].generate_response(
            [Message(role="user", content="test")], response_model=Answer, prompt_name="test.prompt")
        self.assertEqual(result, {"answer": "ok"})
        ranked = await self.clients["cross_encoder"].rank("question", ["a", "b"])
        self.assertEqual(ranked, [("a", 0.9), ("b", 0.9)])
        self.assertEqual(self.counters.prompts["test.prompt"]["calls"], 1)
        self.assertEqual(self.counters.roles["llm"]["requests"], 1)
        self.assertEqual(self.counters.roles["llm"]["input_tokens"], 11)
        self.assertEqual(self.counters.roles["llm"]["output_tokens"], 5)
        self.assertEqual(self.counters.roles["reranker"]["requests"], 2)
        self.assertTrue(all("logprobs" not in p and "logit_bias" not in p for _, p in self.requests))

    async def test_provider_errors_are_counted_and_propagate(self):
        self.status = 403
        with self.assertRaises(Exception):
            await self.clients["embedder"].create("text")
        self.assertEqual(self.counters.roles["embedding"]["http_errors"], 1)


class ConfigTests(unittest.TestCase):
    def test_resume_rejects_model_changes_and_unidentified_existing_graphs(self):
        settings = VertexSettings.from_env(ENV)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(SetupError):
                model_identity(out, settings.profile, settings, {"old-episode"}, False)
            model_identity(out, settings.profile, settings, set(), False)
            model_identity(out, settings.profile, settings, {"episode"}, False)
            changed = replace(settings.profile, embedding_model="different-model-same-dimensions")
            with self.assertRaises(SetupError):
                model_identity(out, changed, settings, {"episode"}, False)
            with self.assertRaises(SetupError):
                model_identity(out, settings.profile, settings, {"episode"}, False, fake=True)
            model_identity(out, changed, settings, set(), True)

    def test_project_models_and_dimensions_are_explicit(self):
        for name in ENV:
            with self.assertRaisesRegex(ConfigError, name):
                VertexSettings.from_env({k: v for k, v in ENV.items() if k != name})
        for dimensions in ("0", "-1", "three"):
            with self.assertRaisesRegex(ConfigError, "positive integer"):
                VertexSettings.from_env({**ENV, "VERTEX_EMBEDDING_DIMENSIONS": dimensions})

    def test_vertex_cannot_run_as_fake_or_with_an_openai_profile(self):
        from graphiti_test.__main__ import main

        for options in (["--vertex", "--fake"], ["--vertex", "--profile", "local-spark"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                main(["run", "--out", "unused", *options])


if __name__ == "__main__":
    unittest.main()
