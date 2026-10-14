"""Embed each node's embed_text and store the vector (ADR 0001)."""

from __future__ import annotations

import hashlib

from ontology.config import check_embedding_identity


# text-embedding-3-small rejects an input over 8192 tokens (HTTP 400, never retried), so one huge description
# would fail its batch on every run. 24000 characters stays under that for ordinary text.
MAX_EMBED_CHARS = 24000


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_graph(writer, client, identity, group_ids: list[str], batch: int = 64) -> int:
    """Embed every node whose text has no up-to-date vector. Returns how many were embedded.

    Refuses (raises EmbeddingMismatch) if the graph already records a different embedding identity.
    """
    recorded = writer.read_embedding_identity()
    if recorded is not None:
        check_embedding_identity(recorded, identity)  # before any API call, so a mismatch spends nothing

    pending = [c for c in writer.embed_candidates(group_ids)
               if c["vector"] is None or c["hash"] != text_hash(c["text"])]
    identity_recorded = recorded is not None
    for start in range(0, len(pending), batch):
        chunk = pending[start:start + batch]
        # The hash covers the full text; only what is sent to the model is cut.
        vectors = client.embed([c["text"][:MAX_EMBED_CHARS] for c in chunk])
        if not identity_recorded:
            # Recorded only after the first successful embed, so a wrong dimension, base URL or profile
            # on the first run cannot lock the graph to an identity that never worked.
            writer.write_embedding_identity(identity)
            identity_recorded = True
        writer.write_embeddings([
            {"label": c["label"], "uuid": c["uuid"], "group_id": c["group_id"],
             "vector": vector, "hash": text_hash(c["text"])}
            for c, vector in zip(chunk, vectors)
        ])
    return len(pending)
