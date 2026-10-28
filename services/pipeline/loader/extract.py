"""extract (EMBER-85): one source_records row to one episode, on ADR 0002's fallback path.

The episode's attrs hold the nodes and edges EMBER-45's mappers produce for the record, already
checked against the ontology (conform.validate_mapped); its text is a short human-readable summary.
Nothing here touches Neo4j; load (load.py) writes the attrs.

Which records map:

- jira issue and comment, slack message, user and channel, and the GitHub kinds in
  mappers.github.RECORD_KINDS (EXTRACTED). Other kinds of those sources (Jira worklogs, Slack
  memberships, GitHub refs, releases, labels and milestones, ...) and other sources (GitLab, Teams)
  are not claimed: the worker counts and logs them at start.
- A deleted record (``record["deleted"]``, a tombstone enrich writes for a Slack message_deleted
  or a Jira *_deleted event; ``record["tombstone"]`` for a GitHub ``deleted`` action) becomes a
  tombstone node: the object's schema key plus deleted: true, stamped with the delete's time. Its
  content is not written, and load never deletes the node.
- A node may carry its own ordering time (``occurred_at`` in attrs): the parent issue or pull request
  a GitHub comment or review carries is stamped with the parent's updated_at, not the comment's time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from ontology import subgraph_id

from .conform import validate_mapped
from .mappers import MapContext, Mapped, Reject, Skip, map_record
from .mappers.base import get, source_node
from .mappers.github import RECORD_KINDS as GITHUB_KINDS
from .mappers.github import github_tombstone, map_github_record
from .mappers.jira import _classify as classify_jira
from .records import EdgeRec, NodeRec

# Bump to re-extract every record: the claim takes rows whose episode has an older version, and
# the upsert replaces that episode and clears its loaded_at, so load writes it again.
EXTRACTOR_VERSION = 1

# (source, kind) of the source_records rows extract claims
EXTRACTED = {("jira", "issue"), ("jira", "comment"), ("slack", "message"), ("slack", "user"), ("slack", "channel"),
             *(("github", kind) for kind in GITHUB_KINDS)}

TEXT_LIMIT = 4000


@dataclass
class SourceRow:
    """The columns of one source_records row extract reads."""

    id: int
    source: str
    tenant: str
    kind: str
    object_id: str
    version: str
    occurred_at: datetime
    record: Any


@dataclass
class EpisodeDraft:
    key: str
    tenant: str
    source: str
    kind: str
    occurred_at: datetime
    text: str
    attrs: dict
    source_record_id: int
    warnings: list[str] = field(default_factory=list)


# Looks up a Jira issue's key by its id among the tenant's stored issue records: (tenant, issue id) -> key
IssueKeys = Callable[[str, str], Awaitable[str | None]]


def episode_key(row: SourceRow) -> str:
    """Unique per source_records row, which is unique on (source, tenant, object_id, version)."""
    return f"{row.tenant}:{row.source}:{row.kind}:{row.object_id}@{row.version}"


def _slack_message(row: SourceRow, record: dict) -> dict:
    """The message as of this version. enrich:slack stores it under "message" (its payload is the
    event, or null for a parent it fetched); the backfill and seed shape put it in "payload"."""
    message = record.get("message") if "message" in record else None
    if message is None:
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "event_callback":
            event = payload.get("event") or {}
            message = event.get("message") if event.get("subtype") == "message_changed" else event
        else:
            message = payload
    if not isinstance(message, dict):
        raise Reject("slack message record has no message")
    message = dict(message)
    message.setdefault("type", "message")
    message.setdefault("channel", row.object_id.partition("/")[0])  # a nested edit omits it
    return message


def _body(row: SourceRow) -> dict:
    """The object one record describes, in a shape EMBER-45's mappers read."""
    record = row.record
    if not isinstance(record, dict):
        raise Reject("record is not a JSON object")
    if row.source == "slack" and row.kind == "message":
        return _slack_message(row, record)
    # enrich:jira and the seed wrap the webhook body in "payload"; enrich:slack stores users and
    # channels as the object itself
    body = record["payload"] if "payload" in record else record
    if not isinstance(body, dict):
        raise Reject("record payload is not a JSON object")
    return body


def _tombstone(row: SourceRow, group_id: str) -> Mapped:
    """The node a delete marks: its schema key and deleted: true, nothing of its content."""
    payload = row.record.get("payload") if isinstance(row.record.get("payload"), dict) else {}
    if row.source == "github":
        return github_tombstone(group_id, row.kind, row.object_id, payload)
    if (row.source, row.kind) == ("slack", "message"):
        label, kind, key = "Message", "message", row.object_id.replace("/", ":", 1)  # the mapper's channel:ts
    elif (row.source, row.kind) == ("jira", "issue"):
        label, kind, key = "WorkItem", "issue", get(payload, "issue", "key")
    elif (row.source, row.kind) == ("jira", "comment"):
        comment_id = get(payload, "comment", "id")
        label, kind, key = "Message", "comment", None if comment_id is None else str(comment_id)
    else:
        raise Skip(f"deleted_{row.kind}")
    if not key:
        raise Reject(f"deleted {row.source} {row.kind} without its id")
    return Mapped(nodes=[source_node(label, group_id, row.source, kind, key, deleted=True)])


async def _issue_keys_for(row: SourceRow, body: dict, issue_keys: IssueKeys) -> dict[str, str]:
    """For a Jira comment, its issue's id -> key, so the mapper can link the comment to it. The
    payload's issue usually carries its key; otherwise the tenant's stored issue records have it."""
    _kind, _comment, issue_id = classify_jira(body)  # a webhook's issue.id, or the REST comment's self URL
    if issue_id is None:
        return {}
    key = get(body, "issue", "key") or get(row.record, "issues", issue_id, "key")
    if not key:
        key = await issue_keys(row.tenant, issue_id)
    return {issue_id: key} if key else {}


def _summary(row: SourceRow, mapped: Mapped, deleted: bool) -> str:
    head = f"{row.source} {row.kind} {row.object_id} at {row.occurred_at.isoformat()}"
    if deleted:
        return f"{head}: deleted"
    main = next((n for n in mapped.nodes if n.props.get("embed_text")), None) or \
        next((n for n in mapped.nodes if n.props.get("name")), None)
    if main is None:
        return head
    text = main.props.get("embed_text") or main.props.get("name")
    return f"{head}\n\n{text}"[:TEXT_LIMIT]


def _node_json(node: NodeRec) -> dict:
    out = {"label": node.label, "key": node.key, "props": node.props}
    if node.occurred_at is not None:  # its own ordering time, not the episode's
        out["occurred_at"] = node.occurred_at
    return out


def _edge_json(edge: EdgeRec) -> dict:
    return {"type": edge.type, "src_label": edge.src_label, "src_key": edge.src_key,
            "dst_label": edge.dst_label, "dst_key": edge.dst_key, "props": edge.props}


async def extract(row: SourceRow, issue_keys: IssueKeys) -> EpisodeDraft:
    """The episode for one record. Raises Skip for a kind that is not mapped and Reject for a
    malformed record; both are permanent."""
    try:
        group_id = subgraph_id(row.tenant, row.source)
    except ValueError as exc:
        raise Reject(str(exc)) from exc
    deleted = isinstance(row.record, dict) and (row.record.get("deleted") is True
                                                or row.record.get("tombstone") is True)
    ctx = MapContext(row.tenant)
    if deleted:
        mapped = _tombstone(row, group_id)
    else:
        body = _body(row)
        if (row.source, row.kind) == ("jira", "comment"):
            ctx.jira_issue_keys.update(await _issue_keys_for(row, body, issue_keys))
        try:
            if row.source == "github":
                mapped = map_github_record(ctx, row.kind, row.object_id, row.record)
            else:
                mapped = map_record(ctx, {"type": row.source, "body": body})
        except (Skip, Reject):
            raise
        except Exception as exc:  # a malformed body fails only itself
            raise Reject(f"mapper error: {type(exc).__name__}: {exc}") from exc
    validate_mapped(mapped, row.source)
    attrs = {
        "group_id": group_id,
        "deleted": deleted,
        "nodes": [_node_json(n) for n in mapped.nodes],
        "edges": [_edge_json(e) for e in mapped.edges],
    }
    return EpisodeDraft(
        key=episode_key(row), tenant=row.tenant, source=row.source, kind=row.kind,
        occurred_at=row.occurred_at, text=_summary(row, mapped, deleted), attrs=attrs,
        source_record_id=row.id, warnings=list(ctx.warnings),
    )


def graph_records(attrs: dict, occurred_at: str) -> tuple[list[NodeRec], list[EdgeRec]]:
    """An episode's attrs back as the records the writers take, stamped with its ordering time (a node
    that carries its own, such as a GitHub comment's parent, keeps that)."""
    group_id = attrs["group_id"]
    nodes = [NodeRec(n["label"], group_id, n["key"], n["props"], occurred_at=n.get("occurred_at") or occurred_at)
             for n in attrs["nodes"]]
    edges = [EdgeRec(e["type"], group_id, e["src_label"], e["src_key"], e["dst_label"], e["dst_key"], e["props"],
                     occurred_at=occurred_at) for e in attrs["edges"]]
    return nodes, edges
