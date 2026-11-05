"""Tenant-scoped experiment export with stable typed keys and explicit embedding identity (EMBER-135)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ontology import parse_subgraph_id

CONTENT = frozenset({"WorkItem", "Change", "Message", "Conversation"})


@dataclass(frozen=True)
class Node:
    ref: str
    source: str
    label: str
    text: str
    url: str = ""
    occurred_at: str = ""
    vector: tuple[float, ...] = ()


@dataclass(frozen=True, order=True)
class Edge:
    src: str
    dst: str
    kind: str


@dataclass(frozen=True)
class Graph:
    tenant: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    identity: str = "none"

    def __post_init__(self):
        if not self.tenant or not self.identity or not self.nodes:
            raise ValueError("graph needs a tenant, embedding identity and nodes")
        refs = {n.ref for n in self.nodes}
        if len(refs) != len(self.nodes):
            raise ValueError("node references must be unique")
        if any(not n.ref.startswith(f"{n.source}:{n.label}:") for n in self.nodes):
            raise ValueError("node reference must include its source and label")
        if any(e.src not in refs or e.dst not in refs for e in self.edges):
            raise ValueError("edge endpoint is outside this tenant's export")
        dims = {len(n.vector) for n in self.nodes if n.vector}
        if len(dims) > 1 or any(not all(math.isfinite(x) for x in n.vector) or n.vector and not any(n.vector) for n in self.nodes):
            raise ValueError("inconsistent or non-finite vectors")
        if dims and self.identity == "none":
            raise ValueError("vectors require an explicit embedding identity")

    @property
    def documents(self):
        return tuple(n for n in self.nodes if n.label in CONTENT and n.text.strip())

    @property
    def fingerprint(self):
        data = asdict(self)
        data["nodes"] = sorted(data["nodes"], key=lambda n: n["ref"])
        data["edges"] = sorted(data["edges"], key=lambda e: (e["src"], e["dst"], e["kind"]))
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def without_pairs(self, pairs):
        hidden = {frozenset(pair) for pair in pairs}
        return replace(self, edges=tuple(e for e in self.edges if frozenset((e.src, e.dst)) not in hidden))

    def save(self, path):
        Path(path).write_text(json.dumps({"format": "ember-search-graph/v1", **asdict(self)}, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.pop("format", None) != "ember-search-graph/v1":
            raise ValueError("unsupported graph export format")
        raw["nodes"] = tuple(Node(**{**n, "vector": tuple(n.get("vector", ()))}) for n in raw["nodes"])
        raw["edges"] = tuple(Edge(**e) for e in raw["edges"])
        return cls(**raw)


def from_memory(writer, tenant: str) -> Graph:
    """Adapter for the existing deterministic loader; no database or model API is called.

    Export only this tenant, and remove tombstones/placeholders before connecting endpoints. Do not export
    Person.email or arbitrary source attributes. Content and URLs still make this a sensitive local artifact.
    """
    nodes, refs = [], {}
    for (label, uuid), props in sorted(writer.nodes.items()):
        owner, source = parse_subgraph_id(props["group_id"])
        if owner != tenant or props.get("deleted") or props.get("placeholder"):
            continue
        # Source-independent nodes have no external_id; their UUID is deterministic in the loader.
        key = props.get("external_id") or uuid
        ref = f"{source}:{label}:{key}"
        refs[(label, uuid)] = ref
        text = props.get("embed_text") or props.get("name") or ""
        vector = props.get("embedding") if props.get("embedding_text_hash") == hashlib.sha256(text.encode()).hexdigest() else ()
        nodes.append(Node(ref, source, label, text, props.get("url") or "", props.get("occurred_at") or "",
                          tuple(vector or ())))
    edges = {Edge(refs[(sl, su)], refs[(dl, du)], kind)
             for (kind, sl, su, dl, du), props in writer.edges.items()
             if (sl, su) in refs and (dl, du) in refs and not props.get("deleted")}
    identity = json.dumps(asdict(writer.identity), sort_keys=True) if writer.identity else "none"
    return Graph(tenant, tuple(sorted(nodes, key=lambda n: n.ref)), tuple(sorted(edges)), identity)


def embed_graph(graph: Graph, embed, identity: str, cache: dict | None = None):
    """Cache raw text embeddings by full model identity and text. Derived neighbour vectors are never cached here."""
    if not identity or identity == "none":
        raise ValueError("embedding identity must describe the configured model")
    cache = {} if cache is None else cache
    documents = graph.documents
    keys = {n.ref: (identity, hashlib.sha256(n.text.encode()).hexdigest()) for n in documents}
    pending = {keys[n.ref]: n.text for n in documents if keys[n.ref] not in cache}
    if pending:
        vectors = embed(list(pending.values()), is_query=False)
        if len(vectors) != len(pending):
            raise ValueError("embedder returned the wrong number of vectors")
        checked = {key: tuple(v) for key, v in zip(pending, vectors, strict=True)}
        if any(not v or not all(math.isfinite(x) for x in v) or not any(v) for v in checked.values()):
            raise ValueError("embedder returned an empty, zero or non-finite vector")
        if len({len(v) for v in checked.values()} | {len(cache[k]) for k in keys.values() if k in cache}) > 1:
            raise ValueError("embedding dimensions changed within one model identity")
        cache.update(checked)
    nodes = tuple(replace(n, vector=cache[keys[n.ref]]) if n.ref in keys else replace(n, vector=()) for n in graph.nodes)
    return replace(graph, nodes=nodes, identity=identity)
