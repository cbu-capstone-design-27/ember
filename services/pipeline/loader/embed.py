"""Embed each node's embed_text and store the vector (ADR 0001)."""

from __future__ import annotations

import hashlib

from ontology.config import check_embedding_identity


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_graph(writer, client, identity, group_ids: list[str], batch: int = 64) -> int:
    """Embed every node whose text has no up-to-date vector. Returns how many were embedded.

    Refuses (raises EmbeddingMismatch) if the graph already records a different embedding identity.
    """
    recorded = writer.read_embedding_identity()
    if recorded is None:
        writer.write_embedding_identity(identity)
    else:
        check_embedding_identity(recorded, identity)

    pending = [c for c in writer.embed_candidates(group_ids)
               if c["vector"] is None or c["hash"] != text_hash(c["text"])]
    for start in range(0, len(pending), batch):
        chunk = pending[start:start + batch]
        vectors = client.embed([c["text"] for c in chunk])
        writer.write_embeddings([
            {"label": c["label"], "uuid": c["uuid"], "group_id": c["group_id"],
             "vector": vector, "hash": text_hash(c["text"])}
            for c, vector in zip(chunk, vectors)
        ])
    return len(pending)
