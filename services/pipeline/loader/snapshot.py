"""A plain view of a loaded graph: source-backed nodes and the edges between them."""

from __future__ import annotations

from dataclasses import dataclass, field

HIDDEN_PROPS = {"embedding", "embedding_text_hash"}


def make_ref(source: str, external_id: str) -> str:
    return f"{source}:{external_id}"


@dataclass
class Snapshot:
    nodes: list[dict] = field(default_factory=list)  # label, ref, source, props
    edges: list[dict] = field(default_factory=list)  # type, src_label, src, dst_label, dst
