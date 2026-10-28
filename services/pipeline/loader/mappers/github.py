"""GitHub objects to graph records: the pull-script REST shapes and webhook shapes of the JSONL
loader (map_github), and the source_records rows enrich:github writes (map_github_record, EMBER-107)."""

from __future__ import annotations

import re
from datetime import datetime

from ontology import subgraph_id

from ..records import EdgeRec, NodeRec, ordering_time
from .base import MapContext, Mapped, Reject, Skip, add_account, get, link_person, source_node

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


# --- source_records rows written by enrich:github (EMBER-107) ---
#
# extract (loader/extract.py) hands each row here as (kind, object_id, record). The record is
# {"payload": <webhook body, or one commit>, "fetched": {...}, "tombstone": true}, the last two only
# when present, as services/github-ingestion/enrich.py's to_records writes it. A node's key is the
# row's object_id wherever the object has one, so a tombstone finds its node from the row alone.

# Kinds that become graph records. ref, release, label and milestone have no home in
# docs/graph-schema.md, so extract does not claim them (loader/extract.py, EXTRACTED).
RECORD_KINDS = ("issue", "pull_request", "issue_comment", "pull_request_review",
                "pull_request_review_comment", "commit_comment", "commit", "repository")


def _ordering(value) -> str | None:
    """A GitHub ISO time as the writers' ordering string (records.ordering_time), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return ordering_time(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _repo_container(gid: str, repo: str, repo_obj: dict | None = None) -> NodeRec:
    url = get(repo_obj, "html_url") or f"https://github.com/{repo}"
    return source_node("Container", gid, "github", "repository", repo, name=repo, url=url)


def _people(out: Mapped, gid: str, obj: dict, node: NodeRec) -> None:
    author = get(obj, "user", "login")
    if author:
        link_person(out, gid, "github", author, node, "AUTHORED", handle=author)
    for assignee in obj.get("assignees") or []:
        login = get(assignee, "login")
        if login:
            link_person(out, gid, "github", login, node, "ASSIGNED_TO", handle=login)


def _work(out: Mapped, gid: str, repo: str, container: NodeRec, obj: dict, *,
          snapshot: bool = False) -> NodeRec:
    """An issue as a WorkItem, or a pull request (also as the issues API shows one, with
    `pull_request` set) as a Change, keyed <repo>#<number>, with CONTAINS and its people.

    A snapshot is the parent a comment or review payload carries. It is stamped with its own
    updated_at, the ordering value the object's own rows use, so it never overrides a newer version
    and fills in a parent whose own events never arrived (one older than the App's install)."""
    number = obj.get("number")
    if number is None:
        raise Reject("github issue or pull request without a number")
    key = f"{repo}#{number}"
    title = obj.get("title") or ""
    text = obj.get("body") or ""
    common = dict(name=title or key, url=obj.get("html_url"), embed_text=f"{title}\n\n{text}".strip() or None,
                  created_at=obj.get("created_at"))
    if "head" in obj or isinstance(obj.get("pull_request"), dict):
        merged_at = obj.get("merged_at") or get(obj, "pull_request", "merged_at")
        node = source_node("Change", gid, "github", "pull_request", key, **common,
                           state="merged" if merged_at else obj.get("state"),
                           source_branch=get(obj, "head", "ref"), target_branch=get(obj, "base", "ref"))
        for closed in _CLOSES.findall(text):
            out.edges.append(EdgeRec("RESOLVES", gid, "Change", key, "WorkItem", f"{repo}#{closed}"))
    else:
        node = source_node("WorkItem", gid, "github", "issue", key, **common,
                           status=obj.get("state"), item_type="issue")
    if snapshot:
        node.occurred_at = _ordering(obj.get("updated_at"))
    out.nodes.append(node)
    out.edges.append(EdgeRec("CONTAINS", gid, "Container", container.key, node.label, key))
    _people(out, gid, obj, node)
    return node


def _directory(path: str) -> str:
    head, _, _ = path.rpartition("/")
    return head or "."


def _touches(out: Mapped, gid: str, repo: str, container: NodeRec, change: NodeRec, paths) -> None:
    """Change TOUCHES Module, one Module per directory that holds a changed file (graph-schema.md:
    a Module is a directory; `path` is repo-relative, "." for the root). Keyed <repo>:<path>."""
    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        directory = _directory(path)
        if directory in seen:
            continue
        seen.add(directory)
        module = NodeRec("Module", gid, f"{repo}:{directory}", {"name": directory, "path": directory})
        out.nodes.append(module)
        out.edges += [EdgeRec("CONTAINS", gid, "Container", container.key, "Module", module.key),
                      EdgeRec("TOUCHES", gid, "Change", change.key, "Module", module.key)]


def _message(out: Mapped, gid: str, key: str, kind: str, obj: dict, created_at=None) -> NodeRec:
    text = obj.get("body") or ""
    node = source_node("Message", gid, "github", kind, key, name=text[:80] or None, url=obj.get("html_url"),
                       embed_text=text or None, created_at=created_at or obj.get("created_at"))
    out.nodes.append(node)
    login = get(obj, "user", "login")
    if login:
        link_person(out, gid, "github", login, node, "AUTHORED", handle=login)
    return node


def github_tombstone(gid: str, kind: str, object_id: str, payload) -> Mapped:
    """The node a GitHub delete marks: its key and deleted: true, nothing of its content."""
    if kind == "issue":
        label, node_kind, key = "WorkItem", "issue", object_id
    elif kind in ("issue_comment", "pull_request_review_comment", "commit_comment"):
        label, node_kind, key = "Message", "comment", object_id
    elif kind == "repository":
        label, node_kind, key = "Container", "repository", get(payload, "repository", "full_name")
    else:
        raise Skip(f"deleted_{kind}")
    if not key:
        raise Reject(f"deleted github {kind} without its id")
    return Mapped(nodes=[source_node(label, gid, "github", node_kind, key, deleted=True)])


def map_github_record(ctx: MapContext, kind: str, object_id: str, record: dict) -> Mapped:
    """One enrich:github source_records row that is not a tombstone, to graph records."""
    if kind not in RECORD_KINDS:
        raise Skip(kind)
    gid = subgraph_id(ctx.tenant, "github")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise Reject("github record has no payload object")
    out = Mapped()
    if kind == "commit":
        return _commit_record(out, gid, object_id, payload)

    repo_obj = payload.get("repository") if isinstance(payload.get("repository"), dict) else None
    repo = get(repo_obj, "full_name")
    if not repo:
        raise Reject(f"github {kind} record without repository.full_name")
    container = _repo_container(gid, repo, repo_obj)
    if kind == "repository":
        out.nodes.append(container)
        return out

    # The row's own object comes first, so the episode summary (extract._summary) is its text.
    if kind in ("issue", "pull_request"):
        obj = payload.get(kind)
        if not isinstance(obj, dict):
            raise Reject(f"github {kind} record without its {kind}")
        node = _work(out, gid, repo, container, obj)
        out.nodes.append(container)
        files = get(record, "fetched", "files")
        if kind == "pull_request" and isinstance(files, list):
            paths = [path for item in files for path in (get(item, "filename"), get(item, "previous_filename"))]
            _touches(out, gid, repo, container, node, paths)
        return out

    if kind == "pull_request_review":
        review, pull = payload.get("review"), payload.get("pull_request")
        if not isinstance(review, dict) or not isinstance(pull, dict):
            raise Reject("github review record without its review and pull request")
        # The review's text is a Message; one with no text (most approvals) is only the REVIEWED edge
        message = None
        if (review.get("body") or "").strip():
            message = _message(out, gid, object_id, "review", review, created_at=review.get("submitted_at"))
        out.nodes.append(container)
        change = _work(out, gid, repo, container, pull, snapshot=True)
        reviewer = get(review, "user", "login")
        if reviewer:
            person = add_account(out, gid, "github", reviewer, handle=reviewer)
            out.edges.append(EdgeRec("REVIEWED", gid, "Person", person.key, "Change", change.key,
                                     {"verdict": (review.get("state") or "").lower() or None}))
        if message is not None:
            out.edges.append(EdgeRec("REFERENCES", gid, "Message", message.key, "Change", change.key))
        return out

    comment = payload.get("comment")
    if not isinstance(comment, dict) or comment.get("id") is None:
        raise Reject(f"github {kind} record without its comment")
    message = _message(out, gid, object_id, "comment", comment)
    out.nodes.append(container)
    if kind == "issue_comment":
        parent = payload.get("issue")
        if not isinstance(parent, dict):
            raise Reject("github issue comment without its issue")
        target = _work(out, gid, repo, container, parent, snapshot=True)
        target_label, target_key = target.label, target.key
    elif kind == "pull_request_review_comment":
        parent = payload.get("pull_request")
        if not isinstance(parent, dict):
            raise Reject("github review comment without its pull request")
        target_label, target_key = "Change", _work(out, gid, repo, container, parent, snapshot=True).key
        reply_to = comment.get("in_reply_to_id")
        if reply_to is not None:
            out.edges.append(EdgeRec("REPLIES_TO", gid, "Message", message.key, "Message",
                                     f"{repo}/review-comment/{reply_to}"))
    else:  # commit_comment: the payload names the commit but does not carry it
        sha = comment.get("commit_id")
        if not sha:
            raise Reject("github commit comment without its commit_id")
        target_label, target_key = "Change", f"{repo}@{sha}"
    out.edges.append(EdgeRec("REFERENCES", gid, "Message", message.key, target_label, target_key))
    return out


def _commit_record(out: Mapped, gid: str, object_id: str, commit: dict) -> Mapped:
    """A commit from a push payload (id, message, timestamp, url, author.username, added /
    modified / removed) or from the compare API (sha, commit.message, html_url, author.login).
    It carries no repository: that is the object_id's prefix, <repo>@<sha>."""
    repo, _, sha_in_id = object_id.rpartition("@")
    sha = commit.get("id") or commit.get("sha") or sha_in_id
    if not repo or not sha:
        raise Reject("github commit without a repository or sha")
    message = commit.get("message") or get(commit, "commit", "message") or ""
    url = commit.get("html_url") or commit.get("url")
    if not (isinstance(url, str) and url.startswith("https://github.com/")):
        url = f"https://github.com/{repo}/commit/{sha}"
    node = source_node("Change", gid, "github", "commit", f"{repo}@{sha}",
                       name=message.splitlines()[0] if message.strip() else sha[:7], url=url,
                       embed_text=message or None,
                       created_at=commit.get("timestamp") or get(commit, "commit", "author", "date"),
                       state="committed")
    container = _repo_container(gid, repo)
    out.nodes += [node, container]
    out.edges.append(EdgeRec("CONTAINS", gid, "Container", container.key, "Change", node.key))
    login = get(commit, "author", "username") or get(commit, "author", "login")
    if login:
        link_person(out, gid, "github", login, node, "AUTHORED", handle=login)
    paths = [*(commit.get("added") or []), *(commit.get("modified") or []), *(commit.get("removed") or [])]
    _touches(out, gid, repo, container, node, paths)
    return out
