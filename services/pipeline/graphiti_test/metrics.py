"""What one run cost: LLM and embedding calls, tokens and failures, counted at the HTTP boundary.

Graphiti's own token_tracker is not fed by OpenAIGenericClient, the client an OpenAI-compatible server uses, so it
cannot give these numbers. Counting at the transport also sees what the prompt-level view hides: the OpenAI SDK's
and Graphiti's retries.
"""

from __future__ import annotations

import contextvars
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

current_prompt: contextvars.ContextVar[str | None] = contextvars.ContextVar("graphiti_test_prompt", default=None)

ROLE_FIELDS = ("requests", "http_errors", "input_tokens", "output_tokens", "inputs")
PROMPT_FIELDS = ("calls", "requests", "input_tokens", "output_tokens")


@dataclass
class Counters:
    roles: dict = field(default_factory=lambda: defaultdict(lambda: dict.fromkeys(ROLE_FIELDS, 0)))
    prompts: dict = field(default_factory=lambda: defaultdict(lambda: dict.fromkeys(PROMPT_FIELDS, 0)))
    structured_failures: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_call(self, prompt: str | None) -> None:
        with self._lock:
            self.prompts[prompt or "unnamed"]["calls"] += 1

    def add_structured_failure(self, prompt: str | None, kind: str, detail: str = "") -> None:
        with self._lock:
            self.structured_failures.append({"prompt": prompt or "unnamed", "kind": kind, "detail": detail})

    def add_request(self, role: str, status: int, usage: dict, inputs: int, prompt: str | None) -> None:
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        with self._lock:
            row = self.roles[role]
            row["requests"] += 1
            row["http_errors"] += status >= 400
            row["input_tokens"] += input_tokens
            row["output_tokens"] += output_tokens
            row["inputs"] += inputs
            if role == "llm":
                by_prompt = self.prompts[prompt or "unnamed"]
                by_prompt["requests"] += 1
                by_prompt["input_tokens"] += input_tokens
                by_prompt["output_tokens"] += output_tokens

    def snapshot(self) -> dict:
        with self._lock:
            return {"roles": {k: dict(v) for k, v in self.roles.items()},
                    "prompts": {k: dict(v) for k, v in self.prompts.items()},
                    "structured_failures": len(self.structured_failures)}


def _subtract(after: dict, before: dict, fields: tuple) -> dict:
    out = {}
    for key, row in after.items():
        base = before.get(key, {})
        diff = {f: row[f] - base.get(f, 0) for f in fields}
        if any(diff.values()):
            out[key] = diff
    return out


def delta(after: dict, before: dict) -> dict:
    """What happened between two snapshots: one episode's or one search's share."""
    return {"roles": _subtract(after["roles"], before["roles"], ROLE_FIELDS),
            "prompts": _subtract(after["prompts"], before["prompts"], PROMPT_FIELDS),
            "structured_failures": after["structured_failures"] - before["structured_failures"]}


class CountingTransport(httpx.AsyncBaseTransport):
    """Counts every request to a model endpoint, retries included. Reads only JSON bodies, never headers or keys."""

    def __init__(self, inner: httpx.AsyncBaseTransport, counters: Counters, role: str) -> None:
        self.inner, self.counters, self.role = inner, counters, role

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        inputs = 0
        if self.role == "embedding":
            try:
                sent = json.loads(request.content).get("input")
                inputs = len(sent) if isinstance(sent, list) else 1
            except ValueError:
                pass
        response = await self.inner.handle_async_request(request)
        await response.aread()
        try:
            usage = json.loads(response.content).get("usage") or {}
        except (ValueError, AttributeError):
            usage = {}
        self.counters.add_request(self.role, response.status_code, usage, inputs, current_prompt.get())
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
