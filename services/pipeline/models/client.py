"""Thin client for OpenAI-compatible chat and embedding endpoints (OpenAI, the Spark service, Gemini's
compatibility endpoint). Retries transient failures and never puts the key in an error message."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .config import Profile

DEFAULT_TASK = "Given a question about a software project, retrieve relevant passages that answer it"
BATCH = 64
# Reasoning models (gpt-5-nano) spend part of the budget on hidden reasoning tokens, so the default is generous.
DEFAULT_MAX_COMPLETION_TOKENS = 1024
_RETRY_STATUS = {429, 500, 502, 503, 504}


class ModelError(RuntimeError):
    pass


class Client:
    def __init__(self, profile: Profile, env=os.environ, *, timeout: float = 60.0, retries: int = 3,
                 sleep=time.sleep) -> None:
        self.profile = profile
        self._env, self._timeout, self._retries, self._sleep = env, timeout, retries, sleep

    def _post(self, base_url: str, path: str, key: str, payload: dict) -> dict:
        url = base_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        for attempt in range(self._retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read(500).decode("utf-8", "replace").replace(key, "***")
                exc.close()
                if exc.code in _RETRY_STATUS and attempt < self._retries:
                    self._sleep(2 ** attempt)
                    continue
                raise ModelError(f"{path} returned HTTP {exc.code}: {body}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self._retries:
                    self._sleep(2 ** attempt)
                    continue
                raise ModelError(f"{path} could not be reached: {exc}") from None
            except json.JSONDecodeError:
                raise ModelError(f"{path} returned something that is not JSON") from None
        raise ModelError(f"{path} failed")  # unreachable; keeps type checkers happy

    def chat(self, messages: list[dict], **options) -> str:
        p = self.profile
        payload = {"model": p.llm_model, "messages": messages}
        if "max_completion_tokens" not in options and "max_tokens" not in options:
            payload["max_completion_tokens"] = DEFAULT_MAX_COMPLETION_TOKENS
        payload.update(options)
        data = self._post(p.llm_base_url, "/chat/completions", p.llm_key(self._env), payload)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ModelError("chat response had no choices[0].message.content") from None

    def embed(self, texts: list[str], *, is_query: bool = False, task: str = DEFAULT_TASK) -> list[list[float]]:
        p = self.profile
        key = p.embedding_key(self._env)
        inputs = [p.query_template.format(task=task, query=t) for t in texts] if is_query else list(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), BATCH):
            batch = inputs[start:start + BATCH]
            payload = {"model": p.embedding_model, "input": batch}
            if p.send_dimensions:
                payload["dimensions"] = p.embedding_dimensions
            rows = sorted(self._post(p.embedding_base_url, "/embeddings", key, payload).get("data", []),
                          key=lambda r: r.get("index", 0))
            if len(rows) != len(batch):
                raise ModelError(f"asked for {len(batch)} embeddings, got {len(rows)}")
            for row in rows:
                vector = row.get("embedding")
                size = len(vector) if isinstance(vector, list) else None
                if size != p.embedding_dimensions:
                    raise ModelError(f"embedding has {size} dimensions but the profile expects "
                                     f"{p.embedding_dimensions}")
                vectors.append(vector)
        return vectors
