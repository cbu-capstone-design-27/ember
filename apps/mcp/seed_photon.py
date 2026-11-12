"""Seed source_records with the Photon synthetic Jira and Slack data, embedded, for trying the MCP server locally.

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
    EMBEDDING_URL=http://<spark>:8000 [EMBEDDING_API_KEY=...] \
        python3 apps/mcp/seed_photon.py
    DATABASE_URL=... python3 apps/mcp/seed_photon.py --no-embed    # rows only, no embedding service needed

EMBER-166: the same import works for your own Jira project and Slack workspace, from the JSONL their pulls
write (services/jira-ingestion/pull_test.py, services/slack-ingestion/pull_test.py), since the photon
fixtures are that output. Until reconcile exists (ADR 0010), this is how history reaches source_records:

    DATABASE_URL=... python3 apps/mcp/seed_photon.py --no-embed --tenant my-org \
        --jira jira.jsonl --slack slack.jsonl

Rows take the shapes the enrich workers store, under tenant "photon" (or --tenant): `{"payload": ...}` for Jira
records and Slack messages, and the users.info and conversations.info object itself for Slack users and channels. Then the
embed worker (services/embed, EMBER-77) embeds every row that isn't yet, so it needs that service's
requirements too. Reruns insert nothing: rows conflict on (source, tenant, object_id, version).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "services" / "pipeline" / "fixtures" / "photon"
COMMENT_ISSUE = re.compile(r"/issue/(\d+)/comment/")
CHANGELOG_ISSUE = re.compile(r"/issue/([^/]+)/changelog")   # the issue's key or id


def bodies(path: Path | None) -> list[dict]:
    return [json.loads(line)["body"] for line in path.read_text().splitlines() if line] if path else []


def jira_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def changelogs(jira: list[dict]) -> dict[str, list[dict]]:
    """Issue key or id -> its changelog entries, from the pull's changelog pages (`values`, or `histories` in the
    expand=changelog shape) in startAt order."""
    pages: dict[str, list[dict]] = {}
    for body in jira:
        match = CHANGELOG_ISSUE.search(body.get("self", ""))
        entries = body.get("values") if isinstance(body.get("values"), list) else body.get("histories")
        if match and isinstance(entries, list):
            pages.setdefault(match.group(1), []).append(body)
    return {issue: [e for page in sorted(found, key=lambda p: p.get("startAt", 0))
                    for e in (page["values"] if isinstance(page.get("values"), list) else page["histories"])]
            for issue, found in pages.items()}


def rows(jira_path: Path | None = FIXTURES / "jira-intake.jsonl", slack_path: Path | None = FIXTURES / "slack-intake.jsonl") -> list[tuple]:
    """(source, kind, object_id, version, occurred_at, record) for every Jira issue, comment and Slack message.
    An issue carries its changelog as issue.changelog.histories, as expand=changelog and Jira reconcile (EMBER-167) store it."""
    out = []
    jira = bodies(jira_path)
    history = changelogs(jira)
    issues = {b["id"]: b for b in jira if "fields" in b and "key" in b}
    for issue in issues.values():
        updated = issue["fields"]["updated"]
        entries = history.get(issue["key"]) or history.get(issue["id"])
        stored = {**issue, "changelog": {"histories": entries}} if entries else issue
        out.append(("jira", "issue", issue["id"], updated, jira_time(updated),
                    {"webhookEvent": "jira:issue_updated", "issue": stored}))
    for comment in (b for b in jira if "author" in b and "body" in b and "/comment/" in b.get("self", "")):
        issue = issues[COMMENT_ISSUE.search(comment["self"]).group(1)]
        out.append(("jira", "comment", f"{issue['id']}/comment/{comment['id']}", comment["updated"], jira_time(comment["updated"]),
                    {"webhookEvent": "comment_created", "comment": comment,
                     "issue": {"id": issue["id"], "key": issue["key"], "self": issue["self"]}}))
    for message in (b for b in bodies(slack_path) if "ts" in b and "text" in b and "channel" in b):
        out.append(("slack", "message", f"{message['channel']}/{message['ts']}", message["ts"],
                    datetime.fromtimestamp(float(message["ts"]), timezone.utc), message))
    return out


def slack_directory(slack_path: Path | None = FIXTURES / "slack-intake.jsonl") -> list[tuple]:
    """(source, kind, object_id, version, occurred_at, record) for every Slack user and channel in the pull, as
    enrich:slack stores what users.info and conversations.info return: the object itself as the record, a user
    versioned by its updated (seconds), a channel by its updated (milliseconds), else its created."""
    out = []
    for body in bodies(slack_path):
        if "profile" in body or "real_name" in body:
            updated = body.get("updated") or 0
            out.append(("slack", "user", body["id"], str(updated), datetime.fromtimestamp(updated, timezone.utc), body))
        elif "is_channel" in body or "is_group" in body:
            updated, created = body.get("updated"), body.get("created")
            at = datetime.fromtimestamp(updated / 1000 if updated else created or 0, timezone.utc)
            out.append(("slack", "channel", body["id"], str(updated or created or 0), at, body))
    return out


async def main(embed_rows: bool = True, tenant: str = "photon", jira: Path | None = None, slack: Path | None = None) -> None:
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    found = rows(jira, slack) if jira or slack else rows()
    directory = slack_directory(slack) if jira or slack else slack_directory()
    await db.executemany(
        "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
        " values ($1, $7, $2, $3, $4, $5, $6) on conflict do nothing",
        [(*row[:5], json.dumps({"payload": row[5]}), tenant) for row in found]
        + [(*row[:5], json.dumps(row[5]), tenant) for row in directory])  # users and channels unwrapped, as enrich:slack
    await db.close()
    if not embed_rows:  # --no-embed: rows only, e.g. for the extract and load workers (EMBER-85)
        return

    # Imported here so rows() works without the embed worker's packages (the EMBER-85 loader test reuses it)
    sys.path.insert(0, str(REPO / "services" / "embed"))
    import embed

    client = embed.EmbeddingClient(os.environ["EMBEDDING_URL"], os.environ.get("EMBEDDING_API_KEY") or None,
                                   os.environ.get("EMBEDDING_MODEL") or embed.DEFAULT_MODEL)
    worker = embed.Worker(os.environ["DATABASE_URL"], client)
    try:
        await worker.check_identity()
        while await worker.embed_batch():
            pass
    finally:
        await worker.engine.dispose()

    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    print(await db.fetch("select source, kind, count(*), count(embedding) as embedded"
                         " from source_records group by 1, 2 order by 1, 2"))
    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-embed", action="store_true", help="rows only, no embedding service needed")
    parser.add_argument("--tenant", default="photon")
    parser.add_argument("--jira", type=Path, help="a Jira pull's JSONL instead of the photon fixture")
    parser.add_argument("--slack", type=Path, help="a Slack pull's JSONL instead of the photon fixture")
    args = parser.parse_args()
    asyncio.run(main(not args.no_embed, args.tenant, args.jira, args.slack))
