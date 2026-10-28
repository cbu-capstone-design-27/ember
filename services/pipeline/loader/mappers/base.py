"""Shared types and helpers for the per-source mappers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..records import EdgeRec, NodeRec


class Skip(Exception):
    """A record of a kind this loader does not map. Counted, not an error."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class Reject(Exception):
    """A record that should have mapped but is malformed. Goes to rejects.jsonl."""


@dataclass
class MapContext:
    tenant: str
    jira_issue_keys: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Mapped:
    nodes: list[NodeRec] = field(default_factory=list)
    edges: list[EdgeRec] = field(default_factory=list)


def get(obj: Any, *path: str) -> Any:
    for part in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def source_node(label, group_id, source, kind, external_id, *, name=None, url=None,
                embed_text=None, created_at=None, **attrs) -> NodeRec:
    return NodeRec(label, group_id, external_id, {
        "source": source, "kind": kind, "external_id": external_id, "name": name, "url": url,
        "embed_text": embed_text, "created_at": created_at, **attrs,
    })


def add_account(out: Mapped, group_id: str, source: str, account_id: str, *,
                handle=None, name=None, email=None) -> NodeRec:
    """Identity plus the Person it resolves to (one per account in phase 1)."""
    identity = source_node("Identity", group_id, source, "user", account_id,
                           name=name or handle, handle=handle)
    person = NodeRec("Person", group_id, f"{source}:{account_id}",
                     {"name": name or handle, "email": email})
    out.nodes += [identity, person]
    out.edges.append(EdgeRec("RESOLVES_TO", group_id, "Identity", identity.key, "Person", person.key))
    return person


def link_person(out: Mapped, group_id: str, source: str, account_id: str, target: NodeRec,
                edge_type: str, *, handle=None, name=None, email=None) -> None:
    person = add_account(out, group_id, source, account_id, handle=handle, name=name, email=email)
    out.edges.append(EdgeRec(edge_type, group_id, "Person", person.key, target.label, target.key))
