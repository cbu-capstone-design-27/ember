"""Read-only retrieval over source_records (EMBER-79).

`search` is Postgres full-text search for now. EMBER-78 replaces its body with pgvector retrieval
and keeps the signature, so the MCP layer in server.py doesn't change.
"""

from __future__ import annotations

import json
import os
from typing import TypedDict

import asyncpg

MAX_RECORD_CHARS = 50_000   # get_record trims a record larger than this


class DatabaseError(Exception):
    """DATABASE_URL is missing or Postgres can't be reached. The message is safe to show the agent."""


class Hit(TypedDict):
    source: str
    kind: str
    object_id: str
    version: str
    occurred_at: str
    title: str
    snippet: str
    url: str | None
    score: float


# The payload object each enrich worker stores in record.payload (GitHub, Jira, GitLab, Slack)
P = "(record->'payload')"


def plain(path: str) -> str:
    """SQL for the text at path in the payload. Jira's REST v3 sends rich text as an ADF document; its text nodes are joined."""
    value = f"{P}#>'{{{path}}}'"
    return (f"case when jsonb_typeof({value}) = 'object' then "
            f"(select string_agg(t #>> '{{}}', ' ') from jsonb_path_query({value}, 'strict $.**.text') t) "
            f"else {value} #>> '{{}}' end")


# Searchable text: titles, bodies, descriptions, message text and commit messages. concat_ws skips nulls.
TEXT = f"""coalesce(nullif(concat_ws(' ',
    {P}#>>'{{pull_request,title}}', {P}#>>'{{pull_request,body}}',
    {P}#>>'{{issue,key}}', {P}#>>'{{issue,title}}', {P}#>>'{{issue,body}}',
    {P}#>>'{{issue,fields,summary}}', {plain('issue,fields,description')},
    {plain('comment,body')}, {P}#>>'{{review,body}}',
    {P}#>>'{{object_attributes,title}}', {P}#>>'{{object_attributes,description}}', {P}#>>'{{object_attributes,note}}',
    {P}->>'message', {P}#>>'{{commit,message}}',
    {P}->>'text', {P}#>>'{{event,text}}', {P}#>>'{{message,text}}'
), ''), record::text)"""

TITLE = f"""left(coalesce(
    {P}#>>'{{pull_request,title}}',
    nullif(concat_ws(' ', {P}#>>'{{issue,key}}', coalesce({P}#>>'{{issue,fields,summary}}', {P}#>>'{{issue,title}}')), ''),
    {P}#>>'{{object_attributes,title}}',
    split_part(coalesce({P}->>'message', {P}#>>'{{commit,message}}'), E'\\n', 1),
    coalesce({P}->>'text', {P}#>>'{{event,text}}', {P}#>>'{{message,text}}'),
    kind || ' ' || object_id
), 120)"""

# Link back to the original. Jira's issue `self` is the REST URL, so browse is built from its site and key.
URL = f"""coalesce(
    {P}#>>'{{comment,html_url}}', {P}#>>'{{review,html_url}}',
    {P}#>>'{{pull_request,html_url}}', {P}#>>'{{issue,html_url}}', {P}->>'html_url',
    substring({P}#>>'{{issue,self}}' from '^https?://[^/]+') || '/browse/' || ({P}#>>'{{issue,key}}'),
    {P}#>>'{{object_attributes,url}}',
    {P}->>'permalink', record#>>'{{fetched,permalink}}',
    case when kind = 'commit' then {P}->>'url' end
)"""

# The newest version of each object. SEARCH then drops it if that version is a delete
NEWEST = """select distinct on (source, tenant, object_id) *
    from source_records
    where $3::text[] is null or source = any($3)
    order by source, tenant, object_id, occurred_at desc, id desc"""

# ponytail: ranks every row per query with no index. Fine for a prototype; EMBER-78 replaces it
SEARCH = f"""
with newest as ({NEWEST}), ranked as (
    select newest.*, body, ts_rank(tsv, q) as score, q
    from newest
    cross join lateral (select {TEXT} as body) b
    cross join lateral (select to_tsvector('english', body) as tsv) v
    cross join websearch_to_tsquery('english', $1) as q
    where tsv @@ q and not (record ? 'tombstone' or record ? 'deleted')
    order by score desc, occurred_at desc
    limit $2
)
select source, kind, object_id, version, occurred_at, {TITLE} as title,
       ts_headline('english', body, q, 'StartSel=**, StopSel=**, MaxFragments=2, MaxWords=30, MinWords=10') as snippet,
       {URL} as url, score
from ranked
order by score desc, occurred_at desc
"""


async def connect() -> asyncpg.Connection:
    """A connection on which every transaction is read-only. The caller closes it."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseError("DATABASE_URL is not set. Point it at Ember's Postgres and restart the server.")
    try:
        return await asyncpg.connect(url, timeout=10, server_settings={"default_transaction_read_only": "on"})
    except (OSError, TimeoutError, asyncpg.PostgresError) as e:
        raise DatabaseError(f"Can't reach Ember's Postgres: {e}") from e


async def search(question: str, k: int = 8, sources: list[str] | None = None) -> list[Hit]:
    """The k source_records that best match question, newest version per object, best first."""
    conn = await connect()
    try:
        rows = await conn.fetch(SEARCH, question, k, sources)
    finally:
        await conn.close()
    return [Hit(**{**dict(row), "occurred_at": row["occurred_at"].isoformat()}) for row in rows]


async def get_record(source: str, object_id: str) -> dict | None:
    """The newest version of one object, record included, or None if it isn't stored."""
    conn = await connect()
    try:
        row = await conn.fetchrow(
            """select source, tenant, kind, object_id, version, occurred_at, record::text as record
               from source_records where source = $1 and object_id = $2
               order by occurred_at desc, id desc limit 1""",
            source, object_id,
        )
    finally:
        await conn.close()
    if row is None:
        return None
    result = dict(row, occurred_at=row["occurred_at"].isoformat())
    if len(row["record"]) > MAX_RECORD_CHARS:
        result["record"] = row["record"][:MAX_RECORD_CHARS]
        result["truncated"] = True
    else:
        result["record"] = json.loads(row["record"])
    return result
