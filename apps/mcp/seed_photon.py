"""Seed source_records with the Photon synthetic Jira and Slack data, embedded, for trying the MCP server locally.

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
    EMBEDDING_URL=http://<spark>:8000 [EMBEDDING_API_KEY=...] \
        python3 apps/mcp/seed_photon.py
    DATABASE_URL=... python3 apps/mcp/seed_photon.py --no-embed    # rows only, no embedding service needed

Rows take the shapes the enrich workers store (`{"payload": ...}`), under tenant "photon". Then the
embed worker (services/embed, EMBER-77) embeds every row that isn't yet, so it needs that service's
requirements too. Reruns insert nothing: rows conflict on (source, tenant, object_id, version).
"""

from __future__ import annotations

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


def bodies(name: str) -> list[dict]:
    return [json.loads(line)["body"] for line in (FIXTURES / name).read_text().splitlines() if line]


def jira_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def rows() -> list[tuple]:
    """(source, kind, object_id, version, occurred_at, record) for every Jira issue, comment and Slack message."""
    out = []
    jira = bodies("jira-intake.jsonl")
    issues = {b["id"]: b for b in jira if "fields" in b and "key" in b}
    for issue in issues.values():
        updated = issue["fields"]["updated"]
        out.append(("jira", "issue", issue["id"], updated, jira_time(updated),
                    {"webhookEvent": "jira:issue_updated", "issue": issue}))
    for comment in (b for b in jira if "author" in b and "body" in b and "/comment/" in b.get("self", "")):
        issue = issues[COMMENT_ISSUE.search(comment["self"]).group(1)]
        out.append(("jira", "comment", f"{issue['id']}/comment/{comment['id']}", comment["updated"], jira_time(comment["updated"]),
                    {"webhookEvent": "comment_created", "comment": comment,
                     "issue": {"id": issue["id"], "key": issue["key"], "self": issue["self"]}}))
    for message in (b for b in bodies("slack-intake.jsonl") if "ts" in b and "text" in b and "channel" in b):
        out.append(("slack", "message", f"{message['channel']}/{message['ts']}", message["ts"],
                    datetime.fromtimestamp(float(message["ts"]), timezone.utc), message))
    return out


async def main(embed_rows: bool = True) -> None:
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    await db.executemany(
        "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
        " values ($1, 'photon', $2, $3, $4, $5, $6) on conflict do nothing",
        [(*row[:5], json.dumps({"payload": row[5]})) for row in rows()])
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
    asyncio.run(main(embed_rows="--no-embed" not in sys.argv[1:]))
