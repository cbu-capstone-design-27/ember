"""Graph-side record of the embedding identity and schema version (ADR 0001, EMBER-34).

Stored as one (:EmberConfig {key: 'embedding'}) node. Services that embed or query
compare it with EXPECTED at startup and refuse to run on a mismatch. A mismatch means
a different model, so the fix is a full rebuild, not a config edit.
"""

from dataclasses import dataclass, fields

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EmbeddingIdentity:
    model: str
    revision: str
    dimension: int
    query_template: str


EXPECTED = EmbeddingIdentity(
    model="Qwen/Qwen3-Embedding-0.6B",
    revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
    dimension=1024,
    query_template="Instruct: {task}\nQuery: {query}",
)


class EmbeddingMismatch(RuntimeError):
    pass


def check_embedding_identity(recorded: EmbeddingIdentity, expected: EmbeddingIdentity = EXPECTED) -> None:
    """Raise EmbeddingMismatch if the graph was built with a different embedding setup."""
    diffs = [
        f"{f.name}: graph has {getattr(recorded, f.name)!r}, service expects {getattr(expected, f.name)!r}"
        for f in fields(EmbeddingIdentity)
        if getattr(recorded, f.name) != getattr(expected, f.name)
    ]
    if diffs:
        raise EmbeddingMismatch(
            "embedding identity mismatch; switching models requires a full rebuild (ADR 0001): "
            + "; ".join(diffs)
        )
