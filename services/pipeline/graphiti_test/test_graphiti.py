"""The parts that import graphiti_core, with the fake models but no database. Skipped if graphiti-core is not installed
(services/pipeline/requirements.txt leaves it out). CI's "Graphiti test harness" step installs it.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

try:
    import graphiti_core
except ImportError:
    graphiti_core = None

from models.config import Profile
from ontology import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

from graphiti_test import fake_models
from graphiti_test.metrics import Counters, current_prompt


def profile(url, send_dimensions=False, dims=32, llm_url=None):
    return Profile(name="test", llm_base_url=llm_url or url, llm_model="fake-chat", llm_key_env="X",
                   embedding_base_url=url, embedding_model="fake-embed", embedding_dimensions=dims,
                   embedding_key_env="Y", send_dimensions=send_dimensions)


@unittest.skipUnless(graphiti_core, "graphiti-core is not installed")
class Clients(unittest.TestCase):
    def build(self, server, **kwargs):
        from graphiti_test.clients import build_clients

        self.counters = Counters()
        return build_clients(profile(server.base_url, **kwargs), fake_models.KEY, fake_models.KEY, self.counters)

    def test_llm_calls_count_by_prompt_with_tokens(self):
        from graphiti_core.prompts.extract_nodes import ExtractedEntities
        from graphiti_core.prompts.models import Message

        async def go(server):
            clients = self.build(server)
            prompt = "<CURRENT MESSAGE>Ryan Stoffel filed PHO-9</CURRENT MESSAGE>"
            return await clients["llm_client"].generate_response(
                [Message(role="system", content="s"), Message(role="user", content=prompt)],
                response_model=ExtractedEntities, prompt_name="extract_nodes.extract_message")

        with fake_models.FakeModels(32) as server:
            result = asyncio.run(go(server))
        self.assertEqual(len(result["extracted_entities"]), 2)
        row = self.counters.prompts["extract_nodes.extract_message"]
        self.assertEqual((row["calls"], row["requests"]), (1, 1))
        self.assertGreater(row["input_tokens"], 0)
        self.assertGreater(row["output_tokens"], 0)
        self.assertEqual(self.counters.structured_failures, [])
        self.assertIsNone(current_prompt.get())

    def _direct(self, server, response_model):
        """Past the retry wrapper, whose backoff is 5 s or more per attempt."""
        from graphiti_core.prompts.models import Message

        async def go():
            clients = self.build(server)
            current_prompt.set("some.prompt")
            return await clients["llm_client"]._generate_response(
                [Message(role="user", content="<CURRENT MESSAGE>x</CURRENT MESSAGE>")], response_model)

        return asyncio.run(go())

    def test_a_reply_that_is_not_json_is_recorded_and_still_raises_as_stock_does(self):
        from graphiti_core.prompts.extract_nodes import ExtractedEntities

        with fake_models.FakeModels(32, bad_json=True) as server:
            with self.assertLogs("graphiti_core", "ERROR"), self.assertRaises(json.JSONDecodeError):
                self._direct(server, ExtractedEntities)
        self.assertEqual(self.counters.structured_failures[0]["kind"], "invalid_json")
        self.assertEqual(self.counters.structured_failures[0]["prompt"], "some.prompt")

    def test_a_reply_off_the_schema_is_recorded_and_passed_on_unchanged(self):
        with fake_models.FakeModels(32, null_attributes=True) as server:
            result = self._direct(server, ENTITY_TYPES["WorkItem"])
        self.assertIsNone(result["external_id"])
        failure = self.counters.structured_failures[0]
        self.assertEqual(failure["kind"], "schema_mismatch")
        self.assertIn("external_id: string_type", failure["detail"])
        self.assertNotIn("fake", failure["detail"])

    def test_dimensions_go_out_only_when_the_profile_says(self):
        seen = []
        real = fake_models.FakeModels.embeddings

        def spy(fake, payload):
            seen.append(payload.get("dimensions"))
            return real(fake, payload)

        fake_models.FakeModels.embeddings = spy
        self.addCleanup(setattr, fake_models.FakeModels, "embeddings", real)

        async def go(server, **kwargs):
            clients = self.build(server, **kwargs)
            vector = await clients["embedder"].create(input_data=["hello"])
            batch = await clients["embedder"].create_batch(["a", "b"])
            return vector, batch

        with fake_models.FakeModels(32) as server:
            vector, batch = asyncio.run(go(server, send_dimensions=True))
            asyncio.run(go(server, send_dimensions=False))
        self.assertEqual(seen, [32, 32, None, None])
        self.assertEqual((len(vector), len(batch), len(batch[0])), (32, 2, 32))
        self.assertEqual(self.counters.roles["embedding"]["inputs"], 3)

    def test_the_cross_encoder_ranks_through_the_fakes_logprobs_and_counts_apart(self):
        async def go(server):
            clients = self.build(server)
            return await clients["cross_encoder"].rank("release stack", ["clipboard history", "the release stack"])

        with fake_models.FakeModels(32) as server:
            ranked = asyncio.run(go(server))
        self.assertEqual(ranked[0][0], "the release stack")
        self.assertEqual(self.counters.roles["reranker"]["requests"], 2)
        self.assertNotIn("llm", self.counters.roles)

    def test_a_server_without_logprobs_fails_the_stock_cross_encoder(self):
        """OpenAIRerankerClient skips a passage with no logprobs and then zips strictly: a ValueError. It also biases
        two OpenAI token ids. What a real run must check on any server that is not OpenAI."""
        real = fake_models.FakeModels.chat

        def no_logprobs(fake, payload):
            reply = real(fake, payload)
            reply["choices"][0]["logprobs"] = None
            return reply

        fake_models.FakeModels.chat = no_logprobs
        self.addCleanup(setattr, fake_models.FakeModels, "chat", real)

        async def go(server):
            return await self.build(server)["cross_encoder"].rank("q", ["a", "b"])

        with fake_models.FakeModels(32) as server:
            with self.assertLogs("graphiti_core", "ERROR"), self.assertRaises(ValueError):
                asyncio.run(go(server))

    def test_api_openai_com_gets_the_responses_client(self):
        from graphiti_test.clients import CountedGenericClient, CountedOpenAIClient, build_llm

        hosted = build_llm(profile("http://x/v1", llm_url="https://api.openai.com/v1"), "k", Counters())
        self.assertIsInstance(hosted, CountedOpenAIClient)
        self.assertIsInstance(build_llm(profile("http://x/v1"), "k", Counters()), CountedGenericClient)


@unittest.skipUnless(graphiti_core, "graphiti-core is not installed")
class Ontology(unittest.TestCase):
    def test_stock_graphiti_accepts_the_ontology(self):
        from graphiti_core.helpers import validate_group_id
        from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types

        self.assertTrue(validate_entity_types(ENTITY_TYPES))
        self.assertTrue(validate_group_id("photon_jira"))
        self.assertTrue(validate_group_id("ember-development_slack"))
        self.assertTrue(all(name in EDGE_TYPES for names in EDGE_TYPE_MAP.values() for name in names))

    def test_a_null_for_a_required_attribute_is_what_fails_the_episode(self):
        """Attribute extraction tells the model to answer null for what it cannot find, and SourceRef's source, kind
        and external_id are required. So stock Graphiti raises on that node, and add_episode with it."""
        from graphiti_core.nodes import EntityNode
        from graphiti_core.utils.maintenance.node_operations import _extract_entity_attributes
        from pydantic import ValidationError

        from graphiti_test.clients import build_llm

        node = EntityNode(name="PHO-1", group_id="g", labels=["Entity", "WorkItem"], summary="")

        async def go(server):
            llm = build_llm(profile(server.base_url), fake_models.KEY, Counters())
            return await _extract_entity_attributes(llm, node, None, [], ENTITY_TYPES["WorkItem"])

        with fake_models.FakeModels(32, null_attributes=True) as server:
            with self.assertRaises(ValidationError):
                asyncio.run(go(server))
        with fake_models.FakeModels(32) as server:
            self.assertEqual(asyncio.run(go(server))["external_id"], "fake-external_id")


class Telemetry(unittest.TestCase):
    def test_a_run_turns_telemetry_off_before_graphiti_loads(self):
        from graphiti_test.run import _graphiti_environment

        before = {k: os.environ.get(k) for k in ("GRAPHITI_TELEMETRY_ENABLED", "EMBEDDING_DIM")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in before.items()])
        os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "true"
        _graphiti_environment(profile("http://x/v1", dims=1024))
        self.assertEqual(os.environ["GRAPHITI_TELEMETRY_ENABLED"], "false")
        self.assertEqual(os.environ["EMBEDDING_DIM"], "1024")


if __name__ == "__main__":
    unittest.main()
