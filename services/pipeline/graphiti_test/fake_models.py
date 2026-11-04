"""A local fake of the chat and embedding endpoints, deterministic, so the whole path runs without real models.

NOT a model: answers come from regexes over the prompts. Output produced with it says nothing about extraction
quality, retrieval quality or cost. It proves plumbing and the graph database. models/fake_server.py is not reused
because it answers every chat "pong" and returns embeddings out of order on purpose, which Graphiti's embedder does
not sort.

Stdlib only. What each prompt must return is keyed on response_format's schema name, which OpenAIGenericClient sets
to the Pydantic class name.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "fake-not-a-secret"

ENTITY_PATTERNS = [
    ("WorkItem", r"\b(?:PHO|GH)-\d+\b"),
    ("Person", r"\b(?:Ryan Stoffel|Maya Okafor|Daniel Reyes)\b"),
    ("Container", r"#(?:photon-[a-z]+|general)\b"),
    ("Change", r"\bv\d+\.\d+\.\d+\b"),
    ("Module", r"\b(?:launcher|clipboard history|Caps Lock|Settings|onboarding|Homebrew)\b"),
]
# The change of fact data.TEMPORAL asks about: (what the episode says, the fact a model would draw from it).
FACT_SUBJECT, FACT_OBJECT, FACT_PREFIX = "Photon", "release stack", "Photon release stack is "
SCRIPTED_FACTS = [
    ("Going to try a Rust + GPUI rewrite", FACT_PREFIX + "Rust + GPUI"),
    ("Decided. GPUI did not pass.", FACT_PREFIX + "Swift 6 + SwiftUI/AppKit"),
]


def _block(text: str, *tags: str) -> str:
    for tag in tags:
        match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
        if match:
            return match.group(1)
    return ""


def _json_block(text: str, *tags: str) -> list:
    """A list a prompt holds: JSON in most blocks, a Python repr in dedupe_edges.resolve_edge's."""
    block = _block(text, *tags) or "[]"
    try:
        return json.loads(block)
    except ValueError:
        pass
    try:
        return ast.literal_eval(block)
    except (ValueError, SyntaxError):
        return []


def _snippet(text: str, words: int = 24) -> str:
    return " ".join(text.split()[:words])


def _facts_in(episode: str) -> list[str]:
    return [fact for phrase, fact in SCRIPTED_FACTS if phrase in episode]


def extract_entities(prompt: str) -> dict:
    types = {name: int(i) for i, name in re.findall(
        r"""entity_type_id['"]?:\s*(\d+),\s*['"]entity_type_name['"]:\s*['"](\w+)""", prompt)}
    episode = _block(prompt, "CURRENT MESSAGE", "JSON", "TEXT")
    found: dict[str, tuple[int, str]] = {}
    for label, pattern in ENTITY_PATTERNS:
        for match in re.finditer(pattern, episode):
            found.setdefault(match.group(0), (match.start(), label))
    if _facts_in(episode):
        found[FACT_SUBJECT], found[FACT_OBJECT] = (-2, "Container"), (-1, "Module")
    ordered = sorted(found.items(), key=lambda item: (item[1][0], item[0]))
    return {"extracted_entities": [{"name": name, "entity_type_id": types.get(label, 0), "episode_indices": [0]}
                                   for name, (_, label) in ordered[:6]]}


def resolve_entities(prompt: str) -> dict:
    existing = {c["name"].lower(): c["candidate_id"] for c in _json_block(prompt, "EXISTING ENTITIES")}
    return {"entity_resolutions": [
        {"id": e["id"], "name": e["name"], "duplicate_candidate_id": existing.get(e["name"].lower(), -1)}
        for e in _json_block(prompt, "ENTITIES")]}


def _relation(types: dict, source: list[str], target: list[str]) -> str:
    for name, signatures in types.items():
        for pair in signatures:
            if pair[0] in source and pair[1] in target:
                return name
    return "REFERENCES"


def extract_edges(prompt: str) -> dict:
    nodes = _json_block(prompt, "ENTITIES")
    episode = _block(prompt, "CURRENT_MESSAGE")
    types = {t["fact_type_name"]: t["fact_type_signatures"] for t in _json_block(prompt, "FACT_TYPES")}
    reference = _block(prompt, "REFERENCE_TIME").split("  #")[0].strip()
    edges = []
    by_name = {n["name"]: n for n in nodes}
    for fact in _facts_in(episode):
        if FACT_SUBJECT in by_name and FACT_OBJECT in by_name:
            edges.append({"source_entity_name": FACT_SUBJECT, "target_entity_name": FACT_OBJECT,
                          "relation_type": _relation(types, by_name[FACT_SUBJECT]["entity_types"],
                                                     by_name[FACT_OBJECT]["entity_types"]),
                          "fact": fact, "valid_at": reference, "invalid_at": None, "episode_indices": [0]})
    plain = [n for n in nodes if n["name"] not in (FACT_SUBJECT, FACT_OBJECT)]
    for node in plain[1:]:
        relation = _relation(types, plain[0]["entity_types"], node["entity_types"])
        phrase = relation.lower().replace("_", " ")
        edges.append({"source_entity_name": plain[0]["name"], "target_entity_name": node["name"],
                      "relation_type": relation,
                      "fact": f"{plain[0]['name']} {phrase} {node['name']}: {_snippet(episode)}",
                      "valid_at": None, "invalid_at": None, "episode_indices": [0]})
    return {"edges": edges}


def resolve_edge(prompt: str) -> dict:
    existing = _json_block(prompt, "EXISTING FACTS")
    candidates = _json_block(prompt, "FACT INVALIDATION CANDIDATES")
    new = _block(prompt, "NEW FACT")
    duplicates = [f["idx"] for f in existing if f["fact"] == new]
    contradicted = [f["idx"] for f in existing + candidates
                    if new.startswith(FACT_PREFIX) and f["fact"].startswith(FACT_PREFIX) and f["fact"] != new]
    return {"duplicate_facts": duplicates, "contradicted_facts": contradicted}


def summarize(prompt: str) -> dict:
    episode = _snippet(_block(prompt, "MESSAGES"), 16)
    return {"summaries": [{"name": e["name"], "summary": f"{e['name']} appears in: {episode}"}
                          for e in _json_block(prompt, "ENTITIES")]}


def timestamps(prompt: str) -> dict:
    return {"valid_at": _block(prompt, "REFERENCE TIME", "REFERENCE_TIME") or None, "invalid_at": None}


def from_schema(schema: dict, nulls: bool = False) -> dict:
    """An instance of any other response model: attribute extraction, for instance. Required fields are filled and
    optional ones left null, or every field null with nulls=True: what the attribute prompt tells a model to do with
    a value it cannot find."""
    out = {}
    required = schema.get("required", [])
    for name, prop in schema.get("properties", {}).items():
        if nulls or name not in required:
            out[name] = None
        else:
            out[name] = {"integer": 0, "number": 0, "boolean": False, "array": [], "object": {}}.get(
                prop.get("type", "string"), f"fake-{name}")
    return out


ANSWERS = {"ExtractedEntities": extract_entities, "NodeResolutions": resolve_entities, "ExtractedEdges": extract_edges,
           "EdgeDuplicate": resolve_edge, "SummarizedEntities": summarize, "EdgeTimestamps": timestamps}


def embedding(text: str, dims: int) -> list[float]:
    """Hashed bag of words, unit length: same words, same direction. Deterministic across processes."""
    vector = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % dims] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        vector[0], norm = 1.0, 1.0
    return [v / norm for v in vector]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _rerank_score(prompt: str) -> float:
    query, passage = _words(_block(prompt, "QUERY")), _words(_block(prompt, "PASSAGE"))
    return max(0.01, min(0.99, len(query & passage) / (len(query) or 1)))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._send(401, {"error": {"message": "bad key"}})
        if self.path.endswith("/chat/completions"):
            return self._send(200, self.server.owner.chat(payload))
        if self.path.endswith("/embeddings"):
            return self._send(200, self.server.owner.embeddings(payload))
        self._send(404, {"error": {"message": f"no such path {self.path}"}})


class FakeModels:
    """An OpenAI-compatible server on 127.0.0.1. `null_attributes` and `bad_json` make it misbehave like a real
    model can, to check what the harness and Graphiti do then. `schemas` counts each response model asked for."""

    def __init__(self, dims: int = 1024, *, null_attributes: bool = False, bad_json: bool = False) -> None:
        self.dims, self.null_attributes, self.bad_json = dims, null_attributes, bad_json
        self.schemas: dict[str, int] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "FakeModels":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def chat(self, payload: dict) -> dict:
        prompt = "\n".join(m["content"] for m in payload["messages"])
        usage = {"prompt_tokens": len(prompt.split()), "completion_tokens": 0}
        if payload.get("logprobs"):  # OpenAIRerankerClient: one True/False token, ranked by its log-probability
            logprob = math.log(_rerank_score(prompt))
            top = [{"token": "True", "logprob": logprob, "bytes": None}]
            choice = {"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "True"},
                      "logprobs": {"content": [{"token": "True", "logprob": logprob, "bytes": None,
                                                "top_logprobs": top}]}}
            usage["completion_tokens"] = 1
            return self._response(payload, choice, usage)
        fmt = (payload.get("response_format") or {}).get("json_schema") or {}
        name, schema = fmt.get("name", ""), fmt.get("schema", {})
        with self._lock:
            self.schemas[name] = self.schemas.get(name, 0) + 1
        if self.bad_json:
            content = "this is not json"
        elif name in ANSWERS:
            content = json.dumps(ANSWERS[name](prompt))
        else:
            content = json.dumps(from_schema(schema, nulls=self.null_attributes))
        usage["completion_tokens"] = len(content.split())
        choice = {"index": 0, "finish_reason": "stop", "logprobs": None,
                  "message": {"role": "assistant", "content": content}}
        return self._response(payload, choice, usage)

    def _response(self, payload: dict, choice: dict, usage: dict) -> dict:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return {"id": "fake", "object": "chat.completion", "created": 0, "model": payload.get("model", "fake"),
                "choices": [choice], "usage": usage}

    def embeddings(self, payload: dict) -> dict:
        texts = payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
        return {"object": "list", "model": payload.get("model", "fake"),
                "data": [{"object": "embedding", "index": i, "embedding": embedding(str(t), self.dims)}
                         for i, t in enumerate(texts)],
                "usage": {"prompt_tokens": sum(len(str(t).split()) for t in texts), "total_tokens": 0}}
