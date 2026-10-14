"""Deterministic stand-ins so tests never call a model provider."""

from __future__ import annotations

import hashlib

from ontology.config import EmbeddingIdentity

from .retrieval import tokenize

BAG_IDENTITY = EmbeddingIdentity("bag-of-words", "test", 64, "{query}")


def bag_of_words_embed(texts: list[str], dims: int = 64, **kwargs) -> list[list[float]]:
    vectors = []
    for text in texts:
        vector = [0.0] * dims
        for token in tokenize(text):
            vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % dims] += 1.0
        vectors.append(vector)
    return vectors


class BagClient:
    def embed(self, texts, **kwargs):
        return bag_of_words_embed(texts)
