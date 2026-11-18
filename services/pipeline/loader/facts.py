"""What a graph contains, as read back from a writer."""

from __future__ import annotations

from dataclasses import dataclass, field

from ontology import ENTITY_TYPES
from ontology.entities import SourceRef

SOURCE_BACKED = {name for name, model in ENTITY_TYPES.items() if issubclass(model, SourceRef)}


@dataclass
class GraphFacts:
    labels: set[str] = field(default_factory=set)
    relationships: dict[tuple[str, str, str], int] = field(default_factory=dict)  # (src, type, dst) -> count
    missing_required: dict[str, int] = field(default_factory=dict)  # placeholders not counted
    # Bare nodes an edge created before (or without) the node's own record (EMBER-85): label -> count.
    # They lack `kind` until filled, so they are reported here, not as missing required attributes.
    placeholders: dict[str, int] = field(default_factory=dict)
    node_counts: dict[str, int] = field(default_factory=dict)
