"""Search methods and metrics for the retrieval suite. Plain Python, no dependencies."""

from __future__ import annotations

import math
import re
from collections import Counter


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = {ref: Counter(tokenize(text)) for ref, text in docs.items()}
        self.length = {ref: sum(tf.values()) for ref, tf in self.tf.items()}
        self.avg = (sum(self.length.values()) / len(self.length)) if self.length else 0.0
        df: Counter = Counter()
        for tf in self.tf.values():
            df.update(tf.keys())
        n = len(docs)
        self.idf = {term: math.log(1 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()}

    def rank(self, query: str, k: int) -> list[str]:
        terms = set(tokenize(query))
        scores: dict[str, float] = {}
        for ref, tf in self.tf.items():
            score = 0.0
            for term in terms:
                freq = tf.get(term, 0)
                if freq:
                    norm = freq + self.k1 * (1 - self.b + self.b * self.length[ref] / self.avg)
                    score += self.idf[term] * freq * (self.k1 + 1) / norm
            if score > 0:
                scores[ref] = score
        return sorted(scores, key=lambda r: (-scores[r], r))[:k]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def vector_rank(query_vector: list[float], vectors: dict[str, list[float]], k: int) -> list[str]:
    scores = {ref: cosine(query_vector, vector) for ref, vector in vectors.items()}
    return sorted(scores, key=lambda r: (-scores[r], r))[:k]


def rrf(rankings: list[list[str]], c: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, ref in enumerate(ranking, start=1):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (c + position)
    return sorted(scores, key=lambda r: (-scores[r], r))


def rank_of(ranked: list[str], answers) -> int | None:
    for position, ref in enumerate(ranked, start=1):
        if ref in answers:
            return position
    return None


def recall_at(ranks: list[int | None], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def mrr(ranks: list[int | None]) -> float:
    return sum(1 / r for r in ranks if r) / len(ranks) if ranks else 0.0
