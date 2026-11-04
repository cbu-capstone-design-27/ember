"""Stock Graphiti's model clients, pointed at a config/models.toml profile and counted. Imports graphiti_core.

No behaviour is changed: the subclasses observe stock calls, except that the embedder sends `dimensions` when the
profile says so (stock never does, and slices the vector instead). Keys are read from the environment variable the
profile names, and only ever passed to the OpenAI client.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.errors import EmptyResponseError
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from openai import AsyncOpenAI
from pydantic import ValidationError

from .metrics import CountingTransport, Counters, current_prompt

TIMEOUT_SECONDS = 600


class _CountedLLM:
    """Names each call's prompt for the transport, and records why a structured response failed.

    Stock clients return the parsed JSON without checking it against response_model; the caller's Model(**response)
    raises later. Validating here only records the mismatch: the response still goes back unchanged, so Graphiti
    behaves as it would. Details hold field paths and error types, never the model's text.
    """

    counters: Counters

    async def generate_response(self, *args, **kwargs):
        prompt = kwargs.get("prompt_name")
        self.counters.add_call(prompt)
        token = current_prompt.set(prompt)
        try:
            return await super().generate_response(*args, **kwargs)
        finally:
            current_prompt.reset(token)

    async def _generate_response(self, messages, response_model=None, *args, **kwargs):
        prompt = current_prompt.get()
        try:
            result = await super()._generate_response(messages, response_model, *args, **kwargs)
        except json.JSONDecodeError:
            self.counters.add_structured_failure(prompt, "invalid_json")
            raise
        except EmptyResponseError:
            self.counters.add_structured_failure(prompt, "empty_response")
            raise
        if response_model is not None:
            try:
                response_model(**result)
            except ValidationError as exc:
                detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()[:5])
                self.counters.add_structured_failure(prompt, "schema_mismatch", f"{response_model.__name__}: {detail}")
        return result


class CountedGenericClient(_CountedLLM, OpenAIGenericClient):
    """For any OpenAI-compatible server: chat.completions with response_format json_schema."""


class CountedOpenAIClient(_CountedLLM, OpenAIClient):
    """For api.openai.com: the Responses API. gpt-5 models reject the max_tokens the generic client sends."""


class ProfileEmbedder(OpenAIEmbedder):
    def __init__(self, config: OpenAIEmbedderConfig, client: AsyncOpenAI, send_dimensions: bool) -> None:
        super().__init__(config, client)
        self.send_dimensions = send_dimensions

    def _options(self) -> dict:
        return {"dimensions": self.config.embedding_dim} if self.send_dimensions else {}

    async def create(self, input_data):
        result = await self.client.embeddings.create(input=input_data, model=self.config.embedding_model,
                                                     **self._options())
        return result.data[0].embedding[: self.config.embedding_dim]

    async def create_batch(self, input_data_list):
        result = await self.client.embeddings.create(input=input_data_list, model=self.config.embedding_model,
                                                     **self._options())
        return [row.embedding[: self.config.embedding_dim] for row in result.data]


def _api(base_url: str, key: str, counters: Counters, role: str) -> AsyncOpenAI:
    transport = CountingTransport(httpx.AsyncHTTPTransport(), counters, role)
    return AsyncOpenAI(api_key=key, base_url=base_url, timeout=TIMEOUT_SECONDS,
                       http_client=httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS))


def build_llm(profile, llm_key: str, counters: Counters, mode: str = "json_schema", client=None) -> LLMClient:
    api = client or _api(profile.llm_base_url, llm_key, counters, "llm")
    # Temperature 0, as a reference has to be repeatable. Stock LLMConfig defaults to 1.
    config = LLMConfig(api_key=llm_key, model=profile.llm_model, small_model=profile.llm_model,
                       base_url=profile.llm_base_url, temperature=0)
    if urlsplit(profile.llm_base_url).hostname == "api.openai.com":
        llm = CountedOpenAIClient(config=config, client=api)
    else:
        llm = CountedGenericClient(config=config, client=api, structured_output_mode=mode)
    llm.counters = counters
    return llm


def build_clients(profile, llm_key: str, embedding_key: str, counters: Counters, mode: str = "json_schema") -> dict:
    """The llm_client, embedder and cross_encoder arguments of Graphiti()."""
    embedder_config = OpenAIEmbedderConfig(embedding_model=profile.embedding_model,
                                           embedding_dim=profile.embedding_dimensions)
    return {
        "llm_client": build_llm(profile, llm_key, counters, mode),
        "embedder": ProfileEmbedder(embedder_config,
                                    _api(profile.embedding_base_url, embedding_key, counters, "embedding"),
                                    profile.send_dimensions),
        "cross_encoder": OpenAIRerankerClient(
            LLMConfig(api_key=llm_key, model=profile.llm_model, base_url=profile.llm_base_url),
            client=_api(profile.llm_base_url, llm_key, counters, "reranker")),
    }
