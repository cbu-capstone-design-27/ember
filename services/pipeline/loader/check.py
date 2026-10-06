"""Compare a loaded graph with the schema and list every mismatch."""

from __future__ import annotations

from ontology import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

from .facts import GraphFacts, expected_constraint_names


def check_graph(facts: GraphFacts) -> list[str]:
    problems: list[str] = []
    for label in sorted(facts.labels - set(ENTITY_TYPES)):
        problems.append(f"unexpected node label {label}")
    for (src, rel, dst), count in sorted(facts.relationships.items()):
        if rel not in EDGE_TYPES:
            problems.append(f"unknown relationship type {rel} ({count} found)")
        elif rel not in EDGE_TYPE_MAP.get((src, dst), []):
            problems.append(f"{rel} not allowed from {src} to {dst} ({count} found)")
    for name in sorted(expected_constraint_names() - facts.constraints):
        problems.append(f"missing constraint {name}")
    for label, count in sorted(facts.missing_required.items()):
        if count:
            problems.append(f"{count} {label} nodes are missing source, kind or external_id")
    return problems
