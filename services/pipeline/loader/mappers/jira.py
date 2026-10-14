"""Jira objects (pull-script REST shapes and webhook shapes) to graph records."""

from __future__ import annotations

import re
from typing import Any

from ontology import subgraph_id

from ..records import EdgeRec
from .base import MapContext, Mapped, Reject, Skip, get, link_person, source_node

_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
_ORIGIN = re.compile(r"^(https://[^/]+)/")
_COMMENT_ISSUE = re.compile(r"/issue/(\d+)/comment/")
_SIBLINGS = {"worklog": "worklog", "attachment": "attachment", "issueLink": "issuelink"}


def adf_text(node: Any) -> str:
    """Plain text from an Atlassian Document Format value, or a plain string."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(t for t in (adf_text(n) for n in node) if t)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return adf_text(node.get("content", []))
    return ""


def _is_issue(obj: Any) -> bool:
    key = obj.get("key") if isinstance(obj, dict) else None
    return isinstance(key, str) and bool(_ISSUE_KEY.fullmatch(key)) and isinstance(obj.get("fields"), dict)


def _classify(body: dict) -> tuple[str, dict, str | None]:
    """(kind, object, parent issue id for comments)."""
    if "webhookEvent" in body:
        # Comment, worklog, attachment and link webhooks carry the parent issue too; they are not it.
        if isinstance(body.get("comment"), dict):
            issue_id = get(body, "issue", "id")
            return "comment", body["comment"], None if issue_id is None else str(issue_id)
        for field_name, kind in _SIBLINGS.items():
            if field_name in body:
                return kind, body, None
        if _is_issue(body.get("issue")):
            return "issue", body["issue"], None
        if isinstance(body.get("issue"), dict):
            return "malformed_issue", body, None
        return "other", body, None
    if _is_issue(body):
        return "issue", body, None
    if "/comment/" in (body.get("self") or "") and "body" in body and "fields" not in body:
        match = _COMMENT_ISSUE.search(body["self"])
        return "comment", body, match.group(1) if match else None
    return "other", body, None


def issue_index(body: dict) -> tuple[str, str] | None:
    """(issue id, issue key) for the comment lookup; a comment webhook indexes its parent issue."""
    kind, obj, _ = _classify(body)
    if kind == "issue" and obj.get("id") is not None:
        return str(obj["id"]), obj["key"]
    issue = body.get("issue") if kind == "comment" else None
    if _is_issue(issue) and issue.get("id") is not None:
        return str(issue["id"]), issue["key"]
    return None


def _origin(self_url: str | None) -> str | None:
    match = _ORIGIN.match(self_url or "")
    return match.group(1) if match else None


def map_jira(ctx: MapContext, body: dict) -> Mapped:
    kind, obj, issue_id = _classify(body)
    if kind == "malformed_issue":
        raise Reject("issue webhook has no usable key/fields")
    if kind not in ("issue", "comment"):
        raise Skip(kind)
    gid = subgraph_id(ctx.tenant, "jira")
    out = Mapped()

    if kind == "issue":
        key = obj["key"]
        fields = obj["fields"]
        origin = _origin(obj.get("self"))
        summary = fields.get("summary") or key
        description = adf_text(fields.get("description"))
        node = source_node(
            "WorkItem", gid, "jira", "issue", key, name=summary,
            url=f"{origin}/browse/{key}" if origin else None,
            embed_text=f"{summary}\n\n{description}".strip(), created_at=fields.get("created"),
            status=get(fields, "status", "name"), item_type=get(fields, "issuetype", "name"),
        )
        out.nodes.append(node)
        project = fields.get("project") or {}
        if project.get("key"):
            container = source_node(
                "Container", gid, "jira", "project", project["key"],
                name=project.get("name") or project["key"],
                url=f"{origin}/projects/{project['key']}" if origin else None,
            )
            out.nodes.append(container)
            out.edges.append(EdgeRec("CONTAINS", gid, "Container", container.key, "WorkItem", key))
        for field_name, edge_type in (("creator", "AUTHORED"), ("assignee", "ASSIGNED_TO")):
            person = fields.get(field_name)
            if isinstance(person, dict) and person.get("accountId"):
                link_person(out, gid, "jira", person["accountId"], node, edge_type,
                            handle=person.get("displayName"), name=person.get("displayName"),
                            email=person.get("emailAddress"))
        for link in fields.get("issuelinks") or []:
            other = link.get("outwardIssue") or link.get("inwardIssue")
            if isinstance(other, dict) and other.get("key"):
                src, dst = (key, other["key"]) if "outwardIssue" in link else (other["key"], key)
                out.edges.append(EdgeRec("RELATES_TO", gid, "WorkItem", src, "WorkItem", dst,
                                         {"link_type": get(link, "type", "name")}))
        return out

    comment_id = obj.get("id")
    if comment_id is None:
        raise Reject("comment has no id")
    text = adf_text(obj.get("body"))
    node = source_node("Message", gid, "jira", "comment", str(comment_id), name=text[:80] or None,
                       embed_text=text, created_at=obj.get("created"))
    out.nodes.append(node)
    issue_key = ctx.jira_issue_keys.get(issue_id) if issue_id else None
    if issue_key:
        out.edges.append(EdgeRec("REFERENCES", gid, "Message", node.key, "WorkItem", issue_key))
    else:
        where = f"id {issue_id}" if issue_id else "id unknown"
        ctx.warnings.append(f"jira comment {comment_id}: its issue ({where}) is not in the input")
    author = obj.get("author")
    if isinstance(author, dict) and author.get("accountId"):
        link_person(out, gid, "jira", author["accountId"], node, "AUTHORED",
                    handle=author.get("displayName"), name=author.get("displayName"),
                    email=author.get("emailAddress"))
    return out
