"""Per-source mapping from launch-source payloads onto the core entity types (EMBER-34).

Adding a source means adding one entry to SOURCES: its kinds (which core type each
kind becomes) and a kind_of function that classifies a raw envelope body. No new
core type is needed unless the source has something that fits none of them.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

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


_GITLAB_HOOK_KINDS = {"merge_request": "merge_request", "issue": "issue", "work_item": "issue", "note": "note"}


def _gitlab(body: dict[str, Any]) -> str | None:
    # Webhook bodies name themselves in object_kind.
    if "object_kind" in body:
        return _GITLAB_HOOK_KINDS.get(body["object_kind"])
    # REST objects from the backfill (services/gitlab-ingestion/pull.py) carry no
    # object_kind, so they are classified by the fields only that object type has.
    if "noteable_type" in body:
        return "note"
    if "iid" in body and "source_branch" in body:
        return "merge_request"
    if "iid" in body and "issue_type" in body:
        return "issue"
    if "path_with_namespace" in body:
        return "project"
    return None


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


# --- One graph per connected scope (EMBER-164) ---
#
# The Postgres graph splits a tenant's source into one graph per scope: a GitHub repo, a GitLab
# project, a Jira project or a Slack workspace. subgraph_id above stays the Neo4j path's group_id.

_REPO = re.compile(r"[^/#@:]+/[^/#@:]+")  # the owner/repo every GitHub object_id starts with
_JIRA_KEY = re.compile(r"([A-Z][A-Z0-9]+)-\d+")


def _get(obj: Any, *path: str) -> Any:
    for part in path:
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def _first(*values: Any) -> str | None:
    return next((v for v in values if isinstance(v, str) and v), None)


def graph_scope(source: str, object_id: str, record: Any, attrs: dict | None = None) -> str | None:
    """The scope of the graph a source_records row (and its episode, attrs) belongs to, or None when
    the record doesn't say. github and code: owner/repo; gitlab: path_with_namespace; both lowercased.
    jira: <site host>/<PROJECT>; slack: the workspace's team id. attrs fills in a Jira comment's
    project, which extract looked up when the payload lacked its issue key."""
    record = record if isinstance(record, dict) else {}
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    if source in ("github", "code"):
        repo = _get(payload, "repository", "full_name")
        match = _REPO.match(object_id)
        found = repo or (match.group(0) if match else None)
        return found.lower() if found else None
    if source == "gitlab":
        path = _first(_get(payload, "project", "path_with_namespace"), payload.get("path_with_namespace"))
        return path.lower() if path else None
    if source == "jira":
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else payload
        self_url = _first(issue.get("self"), _get(payload, "comment", "self"), payload.get("self"))
        host = urlsplit(self_url).hostname if self_url else None
        project = _get(issue, "fields", "project", "key")
        if not project:
            keys = [issue.get("key"), *(e.get("dst_key") for e in (attrs or {}).get("edges", [])),
                    *(n.get("key") for n in (attrs or {}).get("nodes", []) if n.get("label") == "WorkItem")]
            project = next((m.group(1) for k in keys if isinstance(k, str) and (m := _JIRA_KEY.fullmatch(k))), None)
        return f"{host.lower()}/{project}" if host and project else None
    if source == "slack":
        message = record.get("message") if isinstance(record.get("message"), dict) else payload
        return _first(payload.get("team_id"), _get(payload, "event", "team"), message.get("team"),
                      _get(record, "user", "team_id"), _get(record, "channel", "context_team_id"),
                      record.get("team_id"), record.get("context_team_id"))
    return None


def graph_group_id(tenant: str, source: str, graph_id: int) -> str:
    """Graphiti group_id of one graph: {tenant}_{source}_{graphs.id}."""
    if not _TENANT.fullmatch(tenant):
        raise ValueError(f"tenant {tenant!r} must match {_TENANT.pattern}")
    if source not in SOURCES and source != "code":
        raise ValueError(f"unknown source {source!r}")
    return f"{tenant}_{source}_{graph_id}"


async def ensure_graph(conn, tenant: str, source: str, scope: str, name: str | None = None) -> tuple[int, str]:
    """(graphs.id, group_id) of the tenant's graph for one scope, created on first use. conn is an
    asyncpg connection; the id is drawn first so group_id can name it in the same insert."""
    graph_group_id(tenant, source, 0)  # validates tenant and source
    await conn.execute(
        """insert into graph.graphs (id, tenant, source, scope, name, group_id) overriding system value
           select id, $1, $2, $3, $4, $1 || '_' || $2 || '_' || id
             from nextval(pg_get_serial_sequence('graph.graphs', 'id')) id
           on conflict (tenant, source, scope) do nothing""", tenant, source, scope, name or scope)
    row = await conn.fetchrow("select id, group_id from graph.graphs where (tenant, source, scope) = ($1, $2, $3)",
                              tenant, source, scope)
    return row["id"], row["group_id"]
