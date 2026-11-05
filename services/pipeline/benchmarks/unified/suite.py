"""Shared, leakage-checked retrieval evaluation (EMBER-134), separate from the legacy EMBER-58 scores."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .graph import Graph

CATEGORIES = {"single-source", "cross-source", "related"}
SPLITS = {"train", "validation", "test"}


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    answers: tuple[str, ...]
    category: str
    split: str = "validation"
    family: str = ""
    anchor: str | None = None
    hidden_pairs: tuple[tuple[str, str], ...] = ()
    reviewed: bool = False

    def __post_init__(self):
        if type(self.reviewed) is not bool:
            raise ValueError("reviewed must be an explicit boolean")
        if not self.id or not self.text.strip() or not self.answers or len(set(self.answers)) != len(self.answers):
            raise ValueError("a question needs an id, text and unique answer references")
        if self.category not in CATEGORIES or self.split not in SPLITS or not self.family:
            raise ValueError("question needs a known category, split and leakage family")
        if (self.category == "related") != bool(self.anchor):
            raise ValueError("only related-item questions need an anchor")
        if self.anchor in self.answers or any(len(pair) != 2 or pair[0] == pair[1] for pair in self.hidden_pairs):
            raise ValueError("invalid related answer or hidden pair")


def validate_questions(questions):
    ids, texts, families = set(), {}, {}
    for q in questions:
        normalized = " ".join(q.text.casefold().split())
        if q.id in ids or normalized in texts:
            raise ValueError("duplicate question id or text")
        if q.family in families and families[q.family] != q.split:
            raise ValueError("a leakage family appears in multiple splits")
        ids.add(q.id)
        texts[normalized] = q.id
        families[q.family] = q.split


def load_questions(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("format") != "ember-search-questions/v1":
        raise ValueError("unsupported question bank format")
    questions = tuple(Question(**{**q, "answers": tuple(q["answers"]),
                                  "hidden_pairs": tuple(tuple(p) for p in q.get("hidden_pairs", ()))})
                      for q in raw["questions"])
    validate_questions(questions)
    return questions


def metrics(ranked, relevant):
    """Binary set recall and binary nDCG; unjudged documents are treated as nonrelevant."""
    ranked, relevant = list(ranked), set(relevant)
    if not relevant or len(ranked) != len(set(ranked)):
        raise ValueError("metrics need relevance judgments and a duplicate-free ranking")
    hits = [i + 1 for i, ref in enumerate(ranked) if ref in relevant]
    dcg = sum(1 / math.log2(rank + 1) for rank in hits if rank <= 10)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(relevant)) + 1))
    return {"recall@5": len(set(ranked[:5]) & relevant) / len(relevant),
            "recall@10": len(set(ranked[:10]) & relevant) / len(relevant),
            "mrr@10": 1 / hits[0] if hits and hits[0] <= 10 else 0.0,
            "ndcg@10": dcg / ideal}


def percentile(values, fraction):
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def evaluate(graph: Graph, questions, factories, *, split="validation", query_vectors=None,
             query_identity=None, allow_unreviewed=False, unlock_test=False):
    """Factories see only the graph with ALL bank-held-out pairs removed, before any derivation.

    Test execution is explicit. This function does no tuning/training and publishes aggregate numbers only.
    Caller-supplied query vectors must be raw text embeddings from precisely the export's model identity.
    """
    validate_questions(questions)
    if split not in SPLITS or split == "train":
        raise ValueError("evaluate only validation or explicitly unlocked test")
    if split == "test" and not unlock_test:
        raise ValueError("test split is locked; finish validation before explicitly unlocking it")
    selected = [q for q in questions if q.split == split]
    if not selected or not factories:
        raise ValueError("no selected questions or retrieval systems")
    if any(not q.reviewed for q in selected) and not allow_unreviewed:
        raise ValueError("human review required; use allow_unreviewed only for a draft diagnostic")
    if split == "test" and any(not q.reviewed for q in selected):
        raise ValueError("unreviewed questions cannot be used as a test set")
    docs = {n.ref for n in graph.documents}
    for q in questions:
        if set(q.answers) - docs or q.anchor and q.anchor not in docs:
            raise ValueError(f"question {q.id} names an unavailable content document")
        if any(set(pair) - {n.ref for n in graph.nodes} for pair in q.hidden_pairs):
            raise ValueError(f"question {q.id} hides an unavailable endpoint")
    if query_vectors:
        if query_identity != graph.identity or graph.identity == "none":
            raise ValueError("query/document embedding identities differ")
        dims = {len(n.vector) for n in graph.documents if n.vector}
        for vector in query_vectors.values():
            if len(dims) != 1 or len(vector) not in dims or not any(vector) or not all(math.isfinite(x) for x in vector):
                raise ValueError("invalid query embedding")
    hidden = {tuple(sorted(pair)) for q in questions for pair in q.hidden_pairs}
    masked = graph.without_pairs(hidden)
    output = {"format": "ember-search-results/v1", "tenant_fingerprint": hashlib.sha256(graph.tenant.encode()).hexdigest(),
              "graph_fingerprint": graph.fingerprint, "masked_graph_fingerprint": masked.fingerprint,
              "questions_fingerprint": hashlib.sha256(json.dumps([asdict(q) for q in questions], sort_keys=True).encode()).hexdigest(),
              "embedding_identity": graph.identity, "split": split,
              "human_reviewed": all(q.reviewed for q in selected), "hidden_pairs": len(hidden), "systems": {}}
    for name, factory in factories.items():
        started = time.perf_counter()
        system = factory(masked, query_vectors or {})
        build_ms = (time.perf_counter() - started) * 1000
        buckets = {category: [] for category in ("all", *sorted(CATEGORIES))}
        for q in selected:
            started = time.perf_counter()
            result = (system.related(q.anchor, graph.tenant, 10) if q.anchor
                      else system.retrieve(q.text, graph.tenant, 10))
            elapsed = (time.perf_counter() - started) * 1000
            if set(result.refs) - docs or len(result.refs) > 10 or q.anchor in result.refs:
                raise ValueError("retriever returned invalid candidates")
            row = {**metrics(result.refs, q.answers), "latency_ms": elapsed, "llm_calls": result.llm_calls,
                   "input_tokens": result.input_tokens, "output_tokens": result.output_tokens}
            buckets["all"].append(row)
            buckets[q.category].append(row)
        scores = {}
        for category, rows in buckets.items():
            if rows:
                scores[category] = {key: statistics.mean(r[key] for r in rows) for key in metrics([], {"dummy"})}
                scores[category].update(n=len(rows), latency_p50_ms=percentile([r["latency_ms"] for r in rows], .5),
                                        latency_p95_ms=percentile([r["latency_ms"] for r in rows], .95),
                                        llm_calls=sum(r["llm_calls"] for r in rows),
                                        input_tokens=sum(r["input_tokens"] for r in rows),
                                        output_tokens=sum(r["output_tokens"] for r in rows))
        output["systems"][name] = {"build_ms": build_ms, "scores": scores}
    return output
