"""Thin client for OpenAI-compatible chat and embedding endpoints (OpenAI, the Spark service, Gemini's
compatibility endpoint). Retries transient failures and never puts the key in an error message."""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

from .config import Profile

DEFAULT_TASK = "Given a question about a software project, retrieve relevant passages that answer it"
BATCH = 64
# text-embedding-3-small rejects a request over about 300k tokens (HTTP 400, never retried), so a request also
# stops growing at this many characters. A single input is always sent, however long.
MAX_BATCH_CHARS = 200_000
# Reasoning models (gpt-5-nano) spend part of the budget on hidden reasoning tokens, so the default is generous.
DEFAULT_MAX_COMPLETION_TOKENS = 1024
_RETRY_STATUS = {429, 500, 502, 503, 504}


class ModelError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would forward the Authorization header to the redirect target, so a redirect is an error."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _batches(inputs: list[str]):
    """Split into requests of at most BATCH inputs and MAX_BATCH_CHARS characters (a lone input always goes)."""
    batch: list[str] = []
    chars = 0
    for text in inputs:
        if batch and (len(batch) >= BATCH or chars + len(text) > MAX_BATCH_CHARS):
            yield batch
            batch, chars = [], 0
        batch.append(text)
        chars += len(text)
    if batch:
        yield batch


class Client:
    def __init__(self, profile: Profile, env=os.environ, *, timeout: float = 60.0, retries: int = 3,
                 sleep=time.sleep) -> None:
        self.profile = profile
        self._env, self._timeout, self._retries, self._sleep = env, timeout, retries, sleep

    def _post(self, base_url: str, path: str, key: str, payload: dict) -> dict:
        url = base_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        raw = b""
        for attempt in range(self._retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with _OPENER.open(request, timeout=self._timeout) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                try:
                    # Decode and redact before cutting, so a key that straddles the cut is still replaced.
                    body = exc.read(4000).decode("utf-8", "replace").replace(key, "***")[:500]
                except (OSError, http.client.HTTPException):
                    body = ""
                exc.close()
                if exc.code in _RETRY_STATUS and attempt < self._retries:
                    self._sleep(2 ** attempt)
                    continue
                raise ModelError(f"{path} returned HTTP {exc.code}: {body}") from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError and timeouts are OSErrors; a connection dropped while reading the response
                # (RemoteDisconnected, IncompleteRead) is not wrapped by urllib.
                if attempt < self._retries:
                    self._sleep(2 ** attempt)
                    continue
                raise ModelError(f"{path} could not be reached: {exc}") from None
        try:
            result = json.loads(raw.decode("utf-8"))  # ValueError covers bad JSON and bad UTF-8
        except ValueError:
            raise ModelError(f"{path} returned something that is not JSON") from None
        if not isinstance(result, dict):
            raise ModelError(f"{path} returned JSON that is not an object")
        return result

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
        for batch in _batches(inputs):
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
