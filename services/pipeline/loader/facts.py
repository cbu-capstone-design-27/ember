"""What a graph contains (as read back from a writer) and the schema statements to apply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ontology import ENTITY_TYPES
from ontology.entities import SourceRef

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "ontology" / "schema.cypher"
SOURCE_BACKED = {name for name, model in ENTITY_TYPES.items() if issubclass(model, SourceRef)}


@dataclass
class GraphFacts:
    labels: set[str] = field(default_factory=set)
    relationships: dict[tuple[str, str, str], int] = field(default_factory=dict)  # (src, type, dst) -> count
    constraints: set[str] = field(default_factory=set)
    missing_required: dict[str, int] = field(default_factory=dict)  # placeholders not counted
    # Bare nodes an edge created before (or without) the node's own record (EMBER-85): label -> count.
    # They lack `kind` until filled, so they are reported here, not as missing required attributes.
    placeholders: dict[str, int] = field(default_factory=dict)
    node_counts: dict[str, int] = field(default_factory=dict)


def parse_schema_statements(text: str) -> list[str]:
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))
    return [s.strip() for s in body.split(";") if s.strip()]


def expected_constraint_names() -> set[str]:
    return set(re.findall(r"CREATE CONSTRAINT (\w+)", SCHEMA_PATH.read_text(encoding="utf-8")))
