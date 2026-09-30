"""Per-source mapping from launch-source payloads onto the core entity types (EMBER-34).

Adding a source means adding one entry to SOURCES: its kinds (which core type each
kind becomes) and a kind_of function that classifies a raw envelope body. No new
core type is needed unless the source has something that fits none of them.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

from .entities import Change, Container, Conversation, Identity, Message, WorkItem


@dataclass(frozen=True)
class Source:
    name: str
    kinds: dict[str, type[BaseModel]]
    kind_of: Callable[[dict[str, Any]], str | None]


def _github(body: dict[str, Any]) -> str | None:
    if "pull_request" in body or "pull_request" in body.get("issue", {}):
        return "pull_request"
    if "comment" in body:
        return "comment"
    if "issue" in body:
        return "issue"
    return None


def _gitlab(body: dict[str, Any]) -> str | None:
    return {"merge_request": "merge_request", "issue": "issue", "note": "note"}.get(body.get("object_kind"))


def _jira(body: dict[str, Any]) -> str | None:
    if "comment" in body:
        return "comment"
    if "issue" in body:
        return "issue"
    return None


def _slack(body: dict[str, Any]) -> str | None:
    event = body.get("event", {})
    if event.get("type") == "message":
        return "message"
    return None


def _teams(body: dict[str, Any]) -> str | None:
    for change in body.get("value", []):
        if "/messages/" in change.get("resource", ""):
            return "message"
    return None


SOURCES: dict[str, Source] = {
    "github": Source(
        "github",
        {
            "repository": Container, "issue": WorkItem, "pull_request": Change,
            "commit": Change, "comment": Message, "user": Identity,
        },
        _github,
    ),
    "gitlab": Source(
        "gitlab",
        {
            "project": Container, "issue": WorkItem, "merge_request": Change,
            "commit": Change, "note": Message, "user": Identity,
        },
        _gitlab,
    ),
    "jira": Source(
        "jira",
        {"project": Container, "issue": WorkItem, "comment": Message, "user": Identity},
        _jira,
    ),
    "slack": Source(
        "slack",
        {"channel": Container, "thread": Conversation, "message": Message, "user": Identity},
        _slack,
    ),
    "teams": Source(
        "teams",
        {
            "team": Container, "channel": Container, "chat": Conversation,
            "message": Message, "user": Identity,
        },
        _teams,
    ),
}

# Graphiti group_ids allow ASCII letters, digits, '-' and '_'. '_' is reserved as the
# separator, so tenant names use lowercase letters, digits and '-' only.
_TENANT = re.compile(r"[a-z0-9][a-z0-9-]*")


def subgraph_id(tenant: str, source: str) -> str:
    """group_id of the subgraph that holds one source's data for one tenant."""
    if not _TENANT.fullmatch(tenant):
        raise ValueError(f"tenant {tenant!r} must match {_TENANT.pattern}")
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    return f"{tenant}_{source}"


def parse_subgraph_id(group_id: str) -> tuple[str, str]:
    """Inverse of subgraph_id: returns (tenant, source)."""
    tenant, _, source = group_id.rpartition("_")
    subgraph_id(tenant, source)
    return tenant, source


def tenant_subgraphs(tenant: str) -> list[str]:
    """All subgraph ids for a tenant, e.g. for a search across every source."""
    return [subgraph_id(tenant, s) for s in SOURCES]
