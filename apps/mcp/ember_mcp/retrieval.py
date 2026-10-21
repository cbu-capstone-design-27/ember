"""Read-only retrieval over source_records (EMBER-79).

`search` is semantic (EMBER-78): the question is embedded by the embedding service the embed worker
uses (ADR 0001, services/embedding/README.md) and matched against source_records.embedding.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import TypedDict

import asyncpg

MAX_RECORD_CHARS = 50_000   # get_record trims a record larger than this
SNIPPET_CHARS = 300         # a hit's snippet is the start of its embed_text

# services/embedding/README.md, Model identity. Queries get the template, documents never do.
DIMENSION = 1024
DEFAULT_MODEL = "qwen3-embedding-0.6b"
TASK = "Given a question about a software project, retrieve relevant passages that answer it"
ATTEMPTS = 3                # 429, 5xx and network errors are retried after 1 s, then 2 s


class DatabaseError(Exception):
    """DATABASE_URL is missing or Postgres can't be reached. The message is safe to show the agent."""


class EmbeddingError(Exception):
    """The embedding service isn't configured, can't be reached, or returned the wrong vector. Safe to show the agent."""


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

# Nearest embedded rows by cosine distance, ordered on the HNSW index. A row counts only while it is
# its object's newest version and that version isn't a delete. Rows the embed worker hasn't reached are skipped.
SEARCH = f"""
select source, kind, object_id, version, occurred_at, {TITLE} as title,
       left(embed_text, {SNIPPET_CHARS}) as snippet, {URL} as url, 1 - (embedding <=> $1::text::vector) as score
from source_records r
where embedding is not null
  and ($3::text[] is null or source = any($3))
  and not (record ? 'tombstone' or record ? 'deleted')
  and not exists (select 1 from source_records n
                  where (n.source, n.tenant, n.object_id) = (r.source, r.tenant, r.object_id)
                    and (n.occurred_at, n.id) > (r.occurred_at, r.id))
order by embedding <=> $1::text::vector
limit $2
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


def embed_query(question: str) -> list[float]:
    """The question's vector, embedded with ADR 0001's query template. Blocking, standard library only."""
    url, key = os.environ.get("EMBEDDING_URL"), os.environ.get("EMBEDDING_API_KEY")
    if not url or not key:
        raise EmbeddingError("EMBEDDING_URL and EMBEDDING_API_KEY must be set. Point them at Ember's embedding service.")
    body = json.dumps({"model": os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL,
                       "input": [f"Instruct: {TASK}\nQuery: {question}"]}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(ATTEMPTS):
        request = urllib.request.Request(url.rstrip("/") + "/v1/embeddings", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                vector = json.load(response)["data"][0]["embedding"]
            break
        except urllib.error.HTTPError as e:
            e.close()
            if (e.code != 429 and e.code < 500) or attempt == ATTEMPTS - 1:
                raise EmbeddingError(f"The embedding service answered {e.code}: {e.reason}") from e
        except (urllib.error.URLError, OSError) as e:
            if attempt == ATTEMPTS - 1:
                raise EmbeddingError(f"Can't reach the embedding service: {e}") from e
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise EmbeddingError(f"The embedding service sent an unexpected response: {e!r}") from e
        time.sleep(2**attempt)
    if len(vector) != DIMENSION:
        raise EmbeddingError(f"The embedding service returned {len(vector)} dimensions, "
                             f"source_records.embedding is vector({DIMENSION}) (ADR 0001)")
    return vector


async def search(question: str, k: int = 8, sources: list[str] | None = None) -> list[Hit]:
    """The k embedded source_records nearest to question in meaning, newest version per object, best first."""
    vector = await asyncio.to_thread(embed_query, question)
    conn = await connect()
    try:
        # Keep scanning the index past rows the filters drop, so k hits come back when k exist
        await conn.execute("set hnsw.iterative_scan = strict_order")
        rows = await conn.fetch(SEARCH, "[" + ",".join(map(str, vector)) + "]", k, sources)
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
