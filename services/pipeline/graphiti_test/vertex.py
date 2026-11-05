"""Native Vertex AI clients for the stock Graphiti reference test (EMBER-155).

Google ADC owns token refresh. Models and dimensions must be chosen explicitly; nothing here changes the
production embedding space. SDK HTTP traffic is counted by the same transport as the OpenAI profiles.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
from pydantic import ValidationError

from models.config import ConfigError, Profile

from .metrics import CountingTransport, Counters, current_prompt


@dataclass(frozen=True)
class VertexSettings:
    project: str
    location: str
    profile: Profile

    @classmethod
    def from_env(cls, env=os.environ):
        names = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "VERTEX_LLM_MODEL",
                 "VERTEX_EMBEDDING_MODEL", "VERTEX_EMBEDDING_DIMENSIONS")
        values = {name: env.get(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigError("Vertex AI requires: " + ", ".join(missing))
        try:
            dimensions = int(values["VERTEX_EMBEDDING_DIMENSIONS"])
            if dimensions <= 0:
                raise ValueError
        except ValueError:
            raise ConfigError("VERTEX_EMBEDDING_DIMENSIONS must be a positive integer") from None
        project, location = values["GOOGLE_CLOUD_PROJECT"], values["GOOGLE_CLOUD_LOCATION"]
        endpoint = "https://aiplatform.googleapis.com" if location == "global" else f"https://{location}-aiplatform.googleapis.com"
        return cls(project, location, Profile(
            name="vertex-ai", llm_base_url=endpoint, llm_model=values["VERTEX_LLM_MODEL"], llm_key_env="",
            embedding_base_url=endpoint, embedding_model=values["VERTEX_EMBEDDING_MODEL"],
            embedding_dimensions=dimensions, embedding_key_env="", send_dimensions=True, query_template="{query}"))


class CountedGeminiClient(GeminiClient):
    async def _generate_response(self, *args, **kwargs):
        result, input_tokens, output_tokens = await super()._generate_response(*args, **kwargs)
        response_model = kwargs.get("response_model")
        if response_model is not None:
            # Stock Gemini can salvage JSON without validating it. Observe the same failure as the
            # OpenAI harness without changing the response Graphiti sees.
            try:
                response_model.model_validate(result)
            except ValidationError as exc:
                detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()[:5])
                self.counters.add_structured_failure(current_prompt.get(), "schema_mismatch", detail)
        return result, input_tokens, output_tokens

    async def generate_response(self, *args, **kwargs):
        prompt = kwargs.get("prompt_name")
        self.counters.add_call(prompt)
        token = current_prompt.set(prompt)
        try:
            return await super().generate_response(*args, **kwargs)
        finally:
            current_prompt.reset(token)


class VertexEmbedder(GeminiEmbedder):
    """Explicit retrieval tasks, single-text requests, and exact dimensions without silent truncation."""

    async def _embed(self, text: str, task: str):
        result = await self.client.aio.models.embed_content(
            model=self.config.embedding_model, contents=[text],
            config=types.EmbedContentConfig(output_dimensionality=self.config.embedding_dim, task_type=task))
        if not result.embeddings or len(result.embeddings) != 1 or not result.embeddings[0].values:
            raise ValueError("Vertex must return exactly one non-empty embedding")
        vector = result.embeddings[0].values
        if len(vector) != self.config.embedding_dim or not all(math.isfinite(v) for v in vector):
            raise ValueError("Vertex embedding dimensions or values do not match the configured model")
        norm = math.sqrt(sum(v * v for v in vector))
        if not norm or not math.isfinite(norm):
            raise ValueError("Vertex returned a zero embedding")
        return [v / norm for v in vector]

    async def create(self, input_data):
        # Graphiti supplies both strings and singleton lists. Reject ambiguous inputs instead of embedding
        # just their first item, or accidentally sending nested lists to the native SDK.
        if isinstance(input_data, list) and len(input_data) == 1:
            input_data = input_data[0]
        if not isinstance(input_data, str):
            raise ValueError("Vertex create expects one text; use create_batch for multiple texts")
        return await self._embed(input_data, "RETRIEVAL_DOCUMENT")

    async def create_query(self, text):
        return await self._embed(text, "RETRIEVAL_QUERY")

    async def create_batch(self, input_data_list):
        return [await self.create(text) for text in input_data_list]


def build_vertex_clients(settings: VertexSettings, counters: Counters):
    def api(role):
        transport = CountingTransport(httpx.AsyncHTTPTransport(), counters, role)
        # Supply HTTPX explicitly: google-genai can otherwise choose aiohttp when it is installed, which
        # would bypass CountingTransport. No api_key: this is standard Vertex with renewable ADC.
        return genai.Client(vertexai=True, project=settings.project, location=settings.location,
                            http_options=types.HttpOptions(api_version="v1", timeout=600_000,
                                httpx_async_client=httpx.AsyncClient(transport=transport, timeout=600)))

    profile = settings.profile
    llm_config = LLMConfig(model=profile.llm_model, small_model=profile.llm_model, temperature=0)
    llm = CountedGeminiClient(config=llm_config, client=api("llm"))
    llm.counters = counters
    return {"llm_client": llm,
            "embedder": VertexEmbedder(GeminiEmbedderConfig(embedding_model=profile.embedding_model,
                                      embedding_dim=profile.embedding_dimensions), client=api("embedding")),
            "cross_encoder": GeminiRerankerClient(config=llm_config, client=api("reranker"))}


async def close_vertex_clients(graphiti):
    # Graphiti.close only closes its database driver, not these supplied provider clients.
    for component in (graphiti.llm_client, graphiti.embedder, graphiti.cross_encoder):
        await component.client.aio.aclose()
        component.client.close()
