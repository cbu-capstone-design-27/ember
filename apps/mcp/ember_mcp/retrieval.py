"""Read-only retrieval over source_records (EMBER-79) and the Postgres graph (EMBER-166).

Every tool call is one read-only transaction for the user in EMBER_USER_EMAIL (reader()): it resolves
their tenants with ember_user_tenants(), sets `ember.tenants`, and row-level security does the rest.
DATABASE_URL must be a login role in ember_graph_reader (ember_mcp), never the tables' owner.

`search` fuses two ranked lists by reciprocal rank: semantic search over source_records.embedding
(EMBER-78, only when EMBEDDING_URL is set; ADR 0001's query template) and Graphiti's search over the
graph (graph.py). Without EMBEDDING_URL the graph side is BM25 plus a breadth-first walk.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

import asyncpg

from . import graph

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


RRF_K = 60                  # reciprocal rank fusion: score = sum of 1 / (RRF_K + rank)


class AccessError(Exception):
    """EMBER_USER_EMAIL is unset or reaches no tenant, or DATABASE_URL isn't a reader. Safe to show the agent."""


class Fact(TypedDict):
    type: str
    fact: str
    valid_at: str | None


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
    related: list[Fact]


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
# $4, graph ids: only records an episode loaded into one of these graphs.
SEARCH = f"""
select source, kind, object_id, version, occurred_at, {TITLE} as title,
       left(embed_text, {SNIPPET_CHARS}) as snippet, {URL} as url, 1 - (embedding <=> $1::text::vector) as score
from source_records r
where embedding is not null
  and ($3::text[] is null or source = any($3))
  and ($4::bigint[] is null or exists (select from episodes ep where ep.source_record_id = r.id and ep.graph_id = any($4)))
  and not (record ? 'tombstone' or record ? 'deleted')
  and not exists (select 1 from source_records n
                  where (n.source, n.tenant, n.object_id) = (r.source, r.tenant, r.object_id)
                    and (n.occurred_at, n.id) > (r.occurred_at, r.id))
order by embedding <=> $1::text::vector
limit $2
"""

# The newest version of each (source, object_id), as a hit. Deleted objects come back with gone = true.
HITS = f"""
select distinct on (source, object_id) source, kind, object_id, version, occurred_at, {TITLE} as title,
       left(embed_text, {SNIPPET_CHARS}) as snippet, {URL} as url, record ? 'tombstone' or record ? 'deleted' as gone
from source_records where (source, object_id) in (select * from unnest($1::text[], $2::text[]))
order by source, object_id, occurred_at desc, id desc
"""

# What the connected role is. Reads as the tables' owner, a superuser or a role that bypasses row-level
# security would see every tenant, so reader() refuses them.
ROLE_CHECK = """
select pg_has_role('ember_graph_reader', 'usage')
       and not pg_has_role((select relowner from pg_class where oid = 'graph.nodes'::regclass), 'usage')
       and not (select rolsuper or rolbypassrls from pg_roles where rolname = current_user)
"""

GRAPHS = """
select g.id, g.tenant, g.source, g.scope, g.name, g.group_id,
       (select count(*) from graph.nodes n where n.graph_id = g.id and not n.placeholder) as nodes
from graph.graphs g order by g.tenant, g.source, g.scope
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


class Scope:
    """One tool call's reader transaction: the connection, the user and the tenants it may read."""

    def __init__(self, conn: asyncpg.Connection, email: str, tenants: list[str]) -> None:
        self.conn, self.email, self.tenants = conn, email, tenants

    async def graphs(self, sources: list[str] | None = None, names: list[str] | None = None) -> list[asyncpg.Record]:
        """The user's graphs, by source and by name, scope or group_id (case-insensitive)."""
        wanted = {n.lower() for n in names or []}
        return [g for g in await self.conn.fetch(GRAPHS)
                if (sources is None or g["source"] in sources)
                and (names is None or wanted & {g["name"].lower(), g["scope"].lower(), g["group_id"].lower()})]


@asynccontextmanager
async def reader(*, need_tenants: bool = True) -> AsyncIterator[Scope]:
    """A read-only transaction as EMBER_USER_EMAIL: row-level security shows only their tenants' rows,
    and Graphiti runs on the same connection. need_tenants: a user with none is an AccessError."""
    email = (os.environ.get("EMBER_USER_EMAIL") or "").strip()
    if not email:
        raise AccessError("EMBER_USER_EMAIL is not set. Set it to your Ember account's email and restart the server.")
    conn = await connect()
    try:
        async with conn.transaction():
            if not await conn.fetchval(ROLE_CHECK):
                raise AccessError("DATABASE_URL must be a login role in ember_graph_reader (ember_mcp), not the tables' "
                                  "owner or a superuser, so row-level security applies (apps/mcp/README.md).")
            tenants = list(await conn.fetchval("select ember_user_tenants($1)", email))
            if need_tenants and not tenants:
                raise AccessError(f"{email} belongs to no Ember organization, so it can read nothing.")
            await conn.execute("select set_config('ember.tenants', $1, true)", ",".join(tenants))
            async with graph.graphiti().driver.transaction(conn):
                yield Scope(conn, email, tenants)
    finally:
        await conn.close()


def embed_query(question: str) -> list[float]:
    """The question's vector, embedded with ADR 0001's query template. EMBEDDING_API_KEY is sent as a
    bearer token only when set. Blocking, standard library only."""
    url, key = os.environ.get("EMBEDDING_URL"), os.environ.get("EMBEDDING_API_KEY")
    if not url:
        raise EmbeddingError("EMBEDDING_URL is not set. Point it at Ember's embedding service.")
    body = json.dumps({"model": os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL,
                       "input": [f"Instruct: {TASK}\nQuery: {question}"]}).encode()
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {key}"} if key else {})
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


def rrf(lists: list[list[tuple[str, str]]]) -> dict[tuple[str, str], float]:
    """Reciprocal rank fusion of ranked lists of keys, best first. A key counts once per list."""
    scores: dict[tuple[str, str], float] = {}
    for ranked in lists:
        for rank, key in enumerate(dict.fromkeys(ranked)):
            scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
    return dict(sorted(scores.items(), key=lambda item: item[1], reverse=True))


async def search(question: str, k: int = 8, sources: list[str] | None = None,
                 graphs: list[str] | None = None) -> list[Hit]:
    """The k best source records for question, fused from vector search (when EMBEDDING_URL is set) and
    Graphiti's graph search, newest version per object, each with up to five current facts around it."""
    vector = await asyncio.to_thread(embed_query, question) if os.environ.get("EMBEDDING_URL") else None
    async with reader() as scope:
        conn = scope.conn
        group_ids = graph_ids = None   # None: every graph and record the reader sees, link edges included
        if sources is not None or graphs is not None:
            chosen = await scope.graphs(sources, graphs)
            group_ids = [g["group_id"] for g in chosen]
            if graphs is not None:
                if not chosen:
                    return []
                graph_ids = [g["id"] for g in chosen]
        lists = []
        if vector is not None:
            # Keep scanning the index past rows the filters drop, so k hits come back when k exist
            await conn.execute("select set_config('hnsw.iterative_scan', 'strict_order', true)")
            rows = await conn.fetch(SEARCH, "[" + ",".join(map(str, vector)) + "]", k, sources, graph_ids)
            lists.append([(r["source"], r["object_id"]) for r in rows])
        if group_ids != []:  # Graphiti reads [] as every graph
            lists += await graph.search(conn, question, vector, group_ids, max(k, 10))
        fused = rrf(lists)
        keys = [key for key in fused if sources is None or key[0] in sources]
        rows = {(r["source"], r["object_id"]): r for r in await conn.fetch(HITS, [s for s, _ in keys], [o for _, o in keys])}
        keys = [key for key in keys if key in rows and not rows[key]["gone"]][:k]
        nodes = await graph.nodes_of(conn, keys)
        hits = []
        for key in keys:
            row, node = rows[key], nodes.get(key)
            related = [graph.fact(e) for e in await graph.facts(node["uuid"], current=True)][:graph.RELATED] if node else []
            snippet = row["snippet"] or ((node and node["embed_text"]) or "")[:SNIPPET_CHARS]
            hits.append(Hit(source=row["source"], kind=row["kind"], object_id=row["object_id"], version=row["version"],
                            occurred_at=row["occurred_at"].isoformat(), title=row["title"], snippet=snippet,
                            url=row["url"], score=fused[key], related=related))
    return hits


async def _newest(conn: asyncpg.Connection, source: str, object_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """select source, tenant, kind, object_id, version, occurred_at, record::text as record
           from source_records where source = $1 and object_id = $2
           order by occurred_at desc, id desc limit 1""",
        source, object_id,
    )


async def get_record(source: str, object_id: str) -> dict | None:
    """The newest version of one object, record included, with its graph node, the facts that hold
    around it now and its graph. None if the reader can't see it."""
    async with reader() as scope:
        row = await _newest(scope.conn, source, object_id)
        if row is None:
            return None
        node = (await graph.nodes_of(scope.conn, [(source, object_id)])).get((source, object_id))
        found = await graph.graphiti().nodes.entity.get_by_uuid(node["uuid"]) if node else None
        current = await graph.facts(node["uuid"], current=True) if node else []
    result = dict(row, occurred_at=row["occurred_at"].isoformat())
    if len(row["record"]) > MAX_RECORD_CHARS:
        result["record"] = row["record"][:MAX_RECORD_CHARS]
        result["truncated"] = True
    else:
        result["record"] = json.loads(row["record"])
    result["node"] = found and {"label": found.labels[-1], "key": node["key"], "name": found.name, "summary": found.summary}
    result["facts"] = [graph.fact(e) for e in current]
    result["graph"] = node and {"name": node["graph"], "scope": node["scope"], "source": node["graph_source"]}
    return result


def _episode(row: asyncpg.Record, prefix: str) -> dict | None:
    if row[f"{prefix}_episode"] is None:
        return None
    return {"episode": row[f"{prefix}_episode"], "source": row[f"{prefix}_source"],
            "object_id": row[f"{prefix}_object_id"], "version": row[f"{prefix}_version"]}


async def history(source: str, object_id: str) -> list[dict] | None:
    """Every fact about one object's node, ended ones included, oldest first, each with the episode
    (and source record version) that set it and the one that ended it. None if the reader can't see it."""
    async with reader() as scope:
        node = (await graph.nodes_of(scope.conn, [(source, object_id)])).get((source, object_id))
        if node is None:
            return None
        edges = await graph.facts(node["uuid"], current=False)
        provenance = await graph.provenance(scope.conn, edges)
    return [{**graph.fact(e), "invalid_at": e.invalid_at and e.invalid_at.isoformat(),
             "set_by": _episode(provenance[e.uuid], "set"), "ended_by": _episode(provenance[e.uuid], "end")}
            for e in reversed(edges)]


async def whoami() -> dict:
    """The email, the tenants it reads, the graphs it sees and how search runs."""
    async with reader(need_tenants=False) as scope:
        graphs = [dict(g) for g in await scope.graphs()]
    return {"email": scope.email, "tenants": scope.tenants, "graphs": graphs,
            "search": "vector search over source_records and graph nodes and facts, BM25 and graph search, fused"
                      if os.environ.get("EMBEDDING_URL") else
                      "BM25 and graph traversal only: EMBEDDING_URL is not set, so there is no vector search"}
