"""Small sample envelopes in the shapes the connectors write. Used by the tests and for dry runs."""

from __future__ import annotations

import json
from pathlib import Path


def _adf(text: str) -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _env(source: str, body: dict) -> dict:
    return {"type": source, "body": body}


GITHUB = [
    _env("github", {
        "number": 42, "title": "Login redirect drops the query string", "state": "open",
        "html_url": "https://github.com/acme/widget/issues/42", "user": {"login": "octocat"},
        "body": "Repro: sign in with ?next=/settings.", "created_at": "2026-09-23T22:15:00Z",
        "assignees": [{"login": "hubot"}],
    }),
    _env("github", {
        "number": 43, "title": "Fix login redirect", "state": "closed", "merged_at": "2026-09-24T10:00:00Z",
        "html_url": "https://github.com/acme/widget/pull/43", "user": {"login": "hubot"},
        "body": "Closes #42", "head": {"ref": "fix-redirect"}, "base": {"ref": "main"},
        "created_at": "2026-09-23T23:00:00Z",
    }),
    _env("github", {  # the issues API also lists pull requests; this copy is skipped
        "number": 43, "title": "Fix login redirect", "state": "closed", "pull_request": {"url": "x"},
        "html_url": "https://github.com/acme/widget/pull/43", "user": {"login": "hubot"},
    }),
    _env("github", {
        "sha": "abc1234def", "html_url": "https://github.com/acme/widget/commit/abc1234def",
        "commit": {"message": "Fix redirect\n\nKeep the query string.", "author": {"date": "2026-09-23T23:30:00Z"}},
        "author": {"login": "hubot"},
    }),
]

JIRA = [
    _env("jira", {
        "id": "10204", "key": "EMBER-39", "self": "https://ember-capstone.atlassian.net/rest/api/3/issue/10204",
        "fields": {
            "summary": "Define shared payload schema", "issuetype": {"name": "Story"},
            "project": {"key": "EMBER", "name": "Ember"}, "status": {"name": "In Progress"},
            "creator": {"accountId": "acc-1", "displayName": "Reporter Example"},
            "assignee": {"accountId": "acc-2", "displayName": "Assignee Example"},
            "description": _adf("One envelope for every connector."), "created": "2026-09-20T12:00:00.000+0000",
            "issuelinks": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "EMBER-45"}}],
        },
    }),
    _env("jira", {
        "id": "20001", "self": "https://ember-capstone.atlassian.net/rest/api/3/issue/10204/comment/20001",
        "author": {"accountId": "acc-1", "displayName": "Reporter Example"},
        "body": _adf("Shipping the schema today."), "created": "2026-09-21T09:00:00.000+0000",
    }),
]

SLACK = [
    _env("slack", {"id": "T012WORKSPACE", "name": "Acme", "domain": "acme"}),
    _env("slack", {"id": "C012AB3CD", "name": "dev", "is_channel": True, "created": 1717000000}),
    _env("slack", {"id": "U012ABCDEF", "name": "octo", "real_name": "Octo Cat",
                   "profile": {"email": "octo@example.com"}}),
    _env("slack", {"type": "message", "channel": "C012AB3CD", "user": "U012ABCDEF",
                   "text": "Shared intake is type plus body.", "ts": "1717000001.000200",
                   "thread_ts": "1717000001.000200"}),
    _env("slack", {"type": "message", "channel": "C012AB3CD", "user": "U012ABCDEF", "text": "Agreed.",
                   "ts": "1717000002.000300", "thread_ts": "1717000001.000200"}),
]

SAMPLE = GITHUB + JIRA + SLACK
BAD_LINES = ["{not json", json.dumps({"type": "github"})]
BAD_RECORDS = [_env("github", {"number": 7, "title": "Orphan with no repository"})]


def write_sample(root: Path, include_bad: bool = True) -> None:
    for source, records in (("github", GITHUB), ("jira", JIRA), ("slack", SLACK)):
        folder = Path(root) / source
        folder.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records]
        if include_bad and source == "github":
            lines += BAD_LINES + [json.dumps(r) for r in BAD_RECORDS]
        (folder / f"{source}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
