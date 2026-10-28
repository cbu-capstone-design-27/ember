"""GitHub objects (pull-script REST shapes and webhook shapes) to graph records."""

from __future__ import annotations

import re

from ontology import subgraph_id

from ..records import EdgeRec
from .base import MapContext, Mapped, Reject, Skip, get, link_person, source_node

_REPO = re.compile(r"github\.com/([^/]+/[^/]+)/")
_CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")


def _unwrap(body: dict) -> tuple[str, dict, dict | None]:
    if "action" in body:  # webhook
        # Comment and review webhooks carry the parent issue or pull request too; they are not it.
        if "comment" in body:
            return "comment", body, None
        if "review" in body:
            return "review", body, None
        if isinstance(body.get("pull_request"), dict):
            return "pull_request", body["pull_request"], body.get("repository")
        if isinstance(body.get("issue"), dict):
            kind = "issue_is_pull" if "pull_request" in body["issue"] else "issue"
            return kind, body["issue"], body.get("repository")
        return "webhook_other", body, None
    if "title" not in body and "body" in body and ("issue_url" in body or "pull_request_url" in body):
        return "comment", body, None  # bare REST issue or review comment
    if "sha" in body and isinstance(body.get("commit"), dict):
        return "commit", body, None
    if "number" in body and "title" in body and "/milestone/" in (body.get("html_url") or ""):
        return "milestone", body, None
    if "head" in body and "base" in body and "number" in body:
        return "pull_request", body, None
    if "number" in body and "title" in body:
        return ("issue_is_pull" if "pull_request" in body else "issue"), body, None
    return "other", body, None


def _repo_of(url: str | None, repo_obj: dict | None) -> str:
    full = get(repo_obj, "full_name")
    if isinstance(full, str) and full:
        return full
    match = _REPO.search(url or "")
    if match:
        return match.group(1)
    raise Reject("cannot determine repository from html_url or repository.full_name")


def map_github(ctx: MapContext, body: dict) -> Mapped:
    kind, obj, repo_obj = _unwrap(body)
    if kind not in ("issue", "pull_request", "commit"):
        raise Skip(kind)
    gid = subgraph_id(ctx.tenant, "github")
    url = obj.get("html_url")
    repo = _repo_of(url, repo_obj)
    out = Mapped()
    container = source_node("Container", gid, "github", "repository", repo,
                            name=repo, url=f"https://github.com/{repo}")
    out.nodes.append(container)

    if kind == "commit":
        message = get(obj, "commit", "message") or ""
        node = source_node("Change", gid, "github", "commit", f"{repo}@{obj['sha']}",
                           name=message.splitlines()[0] if message else obj["sha"][:7], url=url,
                           embed_text=message, created_at=get(obj, "commit", "author", "date"),
                           state="committed")
        out.nodes.append(node)
        out.edges.append(EdgeRec("CONTAINS", gid, "Container", container.key, "Change", node.key))
        login = get(obj, "author", "login")
        if login:
            link_person(out, gid, "github", login, node, "AUTHORED", handle=login)
        return out

    number = obj.get("number")
    title = obj.get("title") or ""
    text = obj.get("body") or ""
    key = f"{repo}#{number}"
    if kind == "issue":
        node = source_node("WorkItem", gid, "github", "issue", key, name=title, url=url,
                           embed_text=f"{title}\n\n{text}".strip(), created_at=obj.get("created_at"),
                           status=obj.get("state"), item_type="issue")
    else:
        state = "merged" if obj.get("merged_at") else obj.get("state")
        node = source_node("Change", gid, "github", "pull_request", key, name=title, url=url,
                           embed_text=f"{title}\n\n{text}".strip(), created_at=obj.get("created_at"),
                           state=state, source_branch=get(obj, "head", "ref"),
                           target_branch=get(obj, "base", "ref"))
        for closed in _CLOSES.findall(text):
            out.edges.append(EdgeRec("RESOLVES", gid, "Change", key, "WorkItem", f"{repo}#{closed}"))
    out.nodes.append(node)
    out.edges.append(EdgeRec("CONTAINS", gid, "Container", container.key, node.label, key))
    author = get(obj, "user", "login")
    if author:
        link_person(out, gid, "github", author, node, "AUTHORED", handle=author)
    for assignee in obj.get("assignees") or []:
        login = assignee.get("login")
        if login:
            link_person(out, gid, "github", login, node, "ASSIGNED_TO", handle=login)
    return out
