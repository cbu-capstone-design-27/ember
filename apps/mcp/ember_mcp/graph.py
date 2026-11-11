"""Graph retrieval backends (EMBER-122): RETRIEVAL_BACKEND=neo4j or pggraph.

Both seed and expand, with no LLM:

1. The top k hits of the pgvector search (retrieval.nearest) are the seeds.
2. Each seed's node is found through its episode: extract (services/pipeline) writes one `episodes` row per
   source_records row, and the record's own node is the node in `attrs.nodes` whose `props.kind` is the
   episode's kind (a Jira issue's WorkItem, a Slack message's Message). Load writes that node keyed by
   (tenant, source, label, key): in Neo4j as (group_id `<tenant>_<source>`, source, external_id = key), in
   graph.nodes as its (tenant, source, label, key) columns.
3. One hop from the seed's node, over current edges, skipping CONTAINS: a project's or channel's CONTAINS
   edges reach every ticket or thread in it (docs/research/postgres-graph.md, "Hubs in walks"). Placeholder
   and deleted nodes are skipped.
4. A neighbour becomes a hit when it is some record's own node (step 2 in reverse), as that object's newest,
   non-deleted version, under the same source filter. People, statuses and threads have no record of their own.

Ranking: a neighbour scores GRAPH_WEIGHT x its seed's score. Seeds keep their own score. A record reached
more than once keeps its best score, and a graph score that beats it carries `via`. The top k by score come back.
"""

from __future__ import annotations

import os
import re
from typing import Awaitable, Callable

import asyncpg

from .retrieval import SNIPPET_CHARS, TITLE, URL, Hit, Via, connect, hit, nearest

GRAPH_WEIGHT = 0.9
HUB_EDGES = ("CONTAINS",)


class GraphError(Exception):
    """The graph store isn't configured, can't be reached, or lacks its tables. Safe to show the agent."""


# Each seed row's own node, from its episode (module docstring, step 2)
OWN_NODES = """
select e.source_record_id as id, e.tenant, coalesce(n->>'source', e.source) as source, n->>'label' as label, n->>'key' as key
from episodes e cross join jsonb_array_elements(e.attrs->'nodes') n
where e.source_record_id = any($1::bigint[]) and n->'props'->>'kind' = e.kind
"""

# Nodes back to records (step 4): the episodes whose own node is (tenant, source, label, key), then the newest
# version of that object, under SEARCH's rules.
# ponytail: scans episodes' attrs per call, fine at photon size; add an index on the own node's key if it gets slow.
RECORDS = f"""
with k as (select * from unnest($1::text[], $2::text[], $3::text[], $4::text[]) as k(t, s, l, nk)),
own as (
  select distinct k.t, k.s, k.l, k.nk, r.object_id as obj from k
  join episodes e on (e.tenant, e.source) = (k.t, k.s)
   and e.attrs->'nodes' @> jsonb_build_array(jsonb_build_object('label', k.l, 'key', k.nk, 'props', jsonb_build_object('kind', e.kind)))
  join source_records r on r.id = e.source_record_id
)
select own.t, own.s, own.l, own.nk, r.source, kind, object_id, version, occurred_at, {TITLE} as title,
       coalesce(left(embed_text, {SNIPPET_CHARS}), '') as snippet, {URL} as url
from own join source_records r on (r.tenant, r.source, r.object_id) = (own.t, own.s, own.obj)
where not (record ? 'tombstone' or record ? 'deleted')
  and ($5::text[] is null or r.source = any($5))
  and not exists (select 1 from source_records n
                  where (n.source, n.tenant, n.object_id) = (r.source, r.tenant, r.object_id)
                    and (n.occurred_at, n.id) > (r.occurred_at, r.id))
"""

# (tenant, source, label, key) of a node
NodeKey = tuple[str, str, str, str]
# Seed nodes in, (seed index, edge type, neighbour) out
Neighbours = Callable[[asyncpg.Connection, list[NodeKey]], Awaitable[list[tuple[int, str, NodeKey]]]]


async def expand(question: str, k: int, sources: list[str] | None, tenant: str | None, neighbours: Neighbours) -> list[Hit]:
    """Seed and expand (module docstring) with one store's neighbours function."""
    seeds = await nearest(question, k, sources, tenant)
    best: dict[tuple[str, str], Hit] = {(r["source"], r["object_id"]): hit(r) for r in seeds}
    if not seeds:
        return []
    conn = await connect()
    try:
        own = {row["id"]: (row["tenant"], row["source"], row["label"], row["key"])
               for row in await conn.fetch(OWN_NODES, [r["id"] for r in seeds])}
        found = [r for r in seeds if r["id"] in own]
        edges = await neighbours(conn, [own[r["id"]] for r in found])
        keys = sorted({node for _, _, node in edges})
        records = {(row["t"], row["s"], row["l"], row["nk"]): row
                   for row in await conn.fetch(RECORDS, *map(list, zip(*keys)), sources)} if keys else {}
    finally:
        await conn.close()
    for i, edge, node in sorted(edges, key=lambda e: e[0]):
        row, seed = records.get(node), found[i]
        if row is None:
            continue
        score, current = GRAPH_WEIGHT * seed["score"], best.get((row["source"], row["object_id"]))
        if current is None or score > current["score"]:
            best[(row["source"], row["object_id"])] = Hit(
                **hit({**dict(row), "score": score}),
                via=Via(seed_source=seed["source"], seed_object_id=seed["object_id"], edge=edge))
    return sorted(best.values(), key=lambda h: -h["score"])[:k]


# One hop in either direction over current edges (module docstring, step 3)
PG_NEIGHBOURS = """
with s as (select * from unnest($1::int[], $2::text[], $3::text[], $4::text[], $5::text[]) as s(i, tenant, source, label, key))
select s.i, e.type, m.tenant, m.source, m.label, m.key
from s join graph.nodes n on (n.tenant, n.source, n.label, n.key) = (s.tenant, s.source, s.label, s.key) and not n.deleted
cross join lateral (
  select type, dst as other from graph.edges
   where src = n.id and type <> all($6) and invalid_at is null and expired_at is null
  union all
  select type, src from graph.edges
   where dst = n.id and type <> all($6) and invalid_at is null and expired_at is null) e
join graph.nodes m on m.id = e.other and not m.placeholder and not m.deleted
"""


async def pggraph_neighbours(conn: asyncpg.Connection, nodes: list[NodeKey]) -> list[tuple[int, str, NodeKey]]:
    if not await conn.fetchval("select to_regclass('graph.nodes') is not null and to_regclass('graph.edges') is not null"):
        raise GraphError("RETRIEVAL_BACKEND=pggraph needs the graph.nodes and graph.edges tables (ADR 0011, "
                         "docs/research/postgres-graph.md), and this database doesn't have them yet.")
    if not nodes:
        return []
    rows = await conn.fetch(PG_NEIGHBOURS, list(range(len(nodes))), *map(list, zip(*nodes)), list(HUB_EDGES))
    return [(r["i"], r["type"], (r["tenant"], r["source"], r["label"], r["key"])) for r in rows]


async def pggraph_search(question: str, k: int = 8, sources: list[str] | None = None, tenant: str | None = None) -> list[Hit]:
    """Seed and expand over the graph.* tables in Ember's Postgres (ADR 0011)."""
    return await expand(question, k, sources, tenant, pggraph_neighbours)


# Labels can't be Cypher parameters, so the query is built per label. Load writes source-backed nodes on
# (group_id, source, external_id), which schema.cypher's uniqueness constraints index per label.
NEO4J_NEIGHBOURS = """
UNWIND $seeds AS s
MATCH (n:{label} {{group_id: s.group_id, source: s.source, external_id: s.key}})-[r]-(m)
WHERE NOT type(r) IN $hubs AND m.external_id IS NOT NULL
  AND coalesce(n.deleted, false) = false AND coalesce(m.deleted, false) = false AND coalesce(m.placeholder, false) = false
RETURN s.i AS i, type(r) AS type, m.group_id AS group_id, m.source AS source, labels(m)[0] AS label, m.external_id AS key
"""


async def neo4j_neighbours(_conn: asyncpg.Connection, nodes: list[NodeKey]) -> list[tuple[int, str, NodeKey]]:
    """Read-only, with NEO4J_URI, NEO4J_USER (default neo4j), NEO4J_PASSWORD and optionally NEO4J_DATABASE."""
    import neo4j  # only this backend needs the driver

    uri, password = os.environ.get("NEO4J_URI"), os.environ.get("NEO4J_PASSWORD")
    if not uri or not password:
        raise GraphError("RETRIEVAL_BACKEND=neo4j needs NEO4J_URI and NEO4J_PASSWORD. Point them at Ember's Neo4j.")
    by_label: dict[str, list[dict]] = {}
    for i, (tenant, source, label, key) in enumerate(nodes):
        if re.fullmatch(r"[A-Za-z]+", label):
            by_label.setdefault(label, []).append({"i": i, "group_id": f"{tenant}_{source}", "source": source, "key": key})
    if not by_label:
        return []

    async def read(tx):
        out = []
        for label, seeds in by_label.items():
            result = await tx.run(NEO4J_NEIGHBOURS.format(label=label), seeds=seeds, hubs=list(HUB_EDGES))
            out += await result.data()
        return out

    try:
        async with neo4j.AsyncGraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER") or "neo4j", password),
                                                   connection_timeout=10, notifications_min_severity="OFF") as driver:
            async with driver.session(database=os.environ.get("NEO4J_DATABASE") or None,
                                      default_access_mode=neo4j.READ_ACCESS) as session:
                rows = await session.execute_read(read)
    except (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError, OSError, ValueError) as e:
        raise GraphError(f"Can't read Ember's Neo4j: {e}") from e
    # group_id is <tenant>_<source>; tenants never contain '_' (ontology.subgraph_id)
    return [(r["i"], r["type"], (r["group_id"].rpartition("_")[0], r["source"], r["label"], r["key"])) for r in rows]


async def neo4j_search(question: str, k: int = 8, sources: list[str] | None = None, tenant: str | None = None) -> list[Hit]:
    """Seed and expand over Neo4j as services/pipeline's load writes it."""
    return await expand(question, k, sources, tenant, neo4j_neighbours)
