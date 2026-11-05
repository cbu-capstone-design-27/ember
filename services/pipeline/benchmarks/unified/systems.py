"""Executable controls and graph retrieval experiments, with fixed, untuned parameters.

EMBER-137/138/139/140. Training, model-backed rerankers and temporal reconstruction are separate experiments.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import deque
from dataclasses import dataclass

from ..retrieval import BM25, cosine, rrf, vector_rank
from .graph import CONTENT, Graph


@dataclass(frozen=True)
class Result:
    refs: tuple[str, ...]
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def adjacency(graph):
    """Undirected content edges; identity and repository/channel hubs are excluded explicitly."""
    refs = {n.ref for n in graph.nodes if n.label in CONTENT}
    adjacent = {ref: set() for ref in sorted(refs)}
    for edge in graph.edges:
        if edge.src in refs and edge.dst in refs and edge.src != edge.dst:
            adjacent[edge.src].add(edge.dst)
            adjacent[edge.dst].add(edge.src)
    return {ref: tuple(sorted(neighbours)) for ref, neighbours in adjacent.items()}


def pagerank(adj, seeds, restart=0.2, tolerance=1e-10, iterations=200):
    if not 0 < restart <= 1 or not seeds or set(seeds) - set(adj) or any(v < 0 for v in seeds.values()):
        raise ValueError("PPR needs nonnegative seeds in this graph and 0 < restart <= 1")
    total = sum(seeds.values())
    if total <= 0:
        raise ValueError("PPR seeds need positive mass")
    base = {ref: seeds.get(ref, 0) / total for ref in adj}
    scores = dict(base)
    for _ in range(iterations):
        dangling = sum(scores[ref] for ref in adj if not adj[ref])
        updated = {ref: (restart + (1 - restart) * dangling) * base[ref] for ref in adj}
        for ref, neighbours in adj.items():
            if neighbours:
                share = (1 - restart) * scores[ref] / len(neighbours)
                for target in neighbours:
                    updated[target] += share
        difference = sum(abs(updated[ref] - scores[ref]) for ref in adj)
        scores = updated
        if difference < tolerance:
            break
    return scores


def diffuse(adj, features, alpha=0.5, steps=2):
    """Symmetric normalized adjacency with self loops and APPNP-style restart to the input features."""
    if not 0 <= alpha <= 1 or steps < 0:
        raise ValueError("invalid diffusion parameters")
    dim = len(next(iter(features.values())))
    base = {ref: list(features.get(ref, [0.0] * dim)) for ref in adj}
    current = base
    for _ in range(steps):
        updated = {}
        for ref, neighbours in adj.items():
            vector = [0.0] * dim
            for target in (ref, *neighbours):
                weight = 1 / math.sqrt((1 + len(neighbours)) * (1 + len(adj[target])))
                for i, value in enumerate(current[target]):
                    vector[i] += weight * value
            updated[ref] = [(1 - alpha) * value + alpha * own for value, own in zip(vector, base[ref], strict=True)]
        current = updated
    return current


class Retriever:
    """Store-independent experiment API: retrieve(text, tenant, k) and related(ref, tenant, k).

    Shared query vectors are supplied by the runner and must belong to graph.identity. Instances are built
    only after held-out edges have been removed, so derived vectors cannot see the hidden links.
    """
    def __init__(self, graph: Graph, method="hybrid", query_vectors=None, depth=50, seed=132):
        if method not in METHODS or depth < 10:
            raise ValueError("known method and candidate depth >= 10 required")
        self.graph, self.method, self.depth = graph, method, depth
        self.docs = {n.ref: n for n in graph.documents}
        self.vectors = {ref: n.vector for ref, n in self.docs.items()}
        self.query_vectors = dict(query_vectors or {})
        self.bm25 = BM25({ref: n.text for ref, n in self.docs.items()})
        self.adj = adjacency(graph)
        if method != "bm25" and (not self.vectors or any(not v for v in self.vectors.values())):
            raise ValueError(f"{method} needs text vectors for every content document")
        self.smoothed = diffuse(self.adj, self.vectors) if method == "smoothed" else None
        self.structural = None
        if method == "structural":
            features = {}
            for ref in self.adj:
                rng = random.Random(hashlib.sha256(f"{seed}:{ref}".encode()).digest())
                features[ref] = [rng.choice((-1.0, 1.0)) / math.sqrt(32) for _ in range(32)]
            self.structural = diffuse(self.adj, features, alpha=0.2, steps=3)

    def _check(self, tenant, k):
        if tenant != self.graph.tenant:
            raise ValueError("tenant is outside this export")
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ValueError("k must be a positive integer")

    def retrieve(self, question, tenant, k=10):
        self._check(tenant, k)
        lexical = self.bm25.rank(question, self.depth)
        if self.method == "bm25":
            return Result(tuple(lexical[:k]))
        query = self.query_vectors[question]
        if not any(query) or not all(math.isfinite(x) for x in query):
            raise ValueError("query vector must be finite and nonzero")
        semantic = vector_rank(query, self.vectors, self.depth)
        hybrid = rrf([lexical, semantic])[:self.depth]
        if self.method == "vector":
            ranked = semantic
        elif self.method == "hybrid":
            ranked = hybrid
        elif self.method == "ppr":
            scores = pagerank(self.adj, {ref: 1 / (i + 1) for i, ref in enumerate(hybrid[:5])})
            ranked = sorted(self.docs, key=lambda ref: (-scores[ref], ref))
        elif self.method == "fusion":
            distances, queue = {}, deque((ref, 0) for ref in hybrid[:5])
            while queue:
                ref, depth = queue.popleft()
                if ref in distances:
                    continue
                distances[ref] = depth
                if depth < 2:
                    queue.extend((n, depth + 1) for n in self.adj[ref])
            scores = {ref: 0.7 * (1 + cosine(query, self.vectors[ref])) / 2
                      + 0.3 / (1 + distances[ref]) if ref in distances
                      else 0.7 * (1 + cosine(query, self.vectors[ref])) / 2 for ref in hybrid}
            ranked = sorted(hybrid, key=lambda ref: (-scores[ref], ref))
        elif self.method == "smoothed":
            ranked = vector_rank(query, {ref: self.smoothed[ref] for ref in self.docs}, max(k, self.depth))
        elif self.method == "structural":
            weights = [1 / (i + 1) for i in range(len(hybrid[:5]))]
            graph_query = [sum(w * self.structural[ref][i] for w, ref in zip(weights, hybrid[:5])) / sum(weights)
                           for i in range(32)]
            scores = {ref: 0.7 * cosine(query, self.vectors[ref]) + 0.3 * cosine(graph_query, self.structural[ref])
                      for ref in self.docs}
            ranked = sorted(scores, key=lambda ref: (-scores[ref], ref))
        else:
            raise ValueError(f"unknown retrieval method {self.method}")
        return Result(tuple(ranked[:k]))

    def related(self, ref, tenant, k=10):
        self._check(tenant, k)
        if ref not in self.docs:
            raise ValueError("related anchor is not a content document")
        # Same query path as a natural-language question, with the anchor's vector in text space.
        text = self.docs[ref].text
        previous = self.query_vectors.get(text)
        self.query_vectors[text] = self.vectors[ref]
        try:
            ranked = self.retrieve(text, tenant, k + 1).refs
            return Result(tuple(r for r in ranked if r != ref)[:k])
        finally:
            if previous is None:
                self.query_vectors.pop(text, None)
            else:
                self.query_vectors[text] = previous


METHODS = ("bm25", "vector", "hybrid", "fusion", "ppr", "smoothed", "structural")


def registry(methods=METHODS):
    """Add another method by supplying a {name: factory(graph, query_vectors)} mapping to the runner."""
    if set(methods) - set(METHODS):
        raise ValueError("unknown method")
    return {name: (lambda graph, vectors, method=name: Retriever(graph, method, vectors)) for name in methods}
