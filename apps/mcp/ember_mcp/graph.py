"""The Postgres graph through the Graphiti fork (EMBER-166, docs/research/postgres-graph.md Part 2).

Every call here runs inside retrieval.reader()'s transaction: role ember_graph_reader with
`ember.tenants` set, so row-level security limits the graph to the user's tenants. Graphiti runs on
PostgresDriver over that same connection.

A source record and its graph node are joined through the record's episodes: a record's node is the
first WorkItem, Change or Message in its episode's attrs.nodes (the mappers put the row's own object
first; a Slack message's Container and Conversation come before its Message).
"""

from __future__ import annotations

import re

import asyncpg
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.postgres_driver import PostgresDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client import LLMConfig, OpenAIClient
from graphiti_core.search.search_config import (
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    EpisodeReranker,
    EpisodeSearchConfig,
    EpisodeSearchMethod,
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
)
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF

RELATED = 5   # facts per search hit

# Identifiers a question can name: a Jira key, a GitHub owner/repo#n, or a bare #n
JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+\b")
REPO_NUMBER = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
BARE_NUMBER = re.compile(r"(?<![\w/.#-])#(\d+)\b")

# Without EMBEDDING_URL: BM25 over nodes, facts and episodes, then a breadth-first walk from what it found
TEXT_AND_TRAVERSAL = SearchConfig(
    edge_config=EdgeSearchConfig(search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.bfs], reranker=EdgeReranker.rrf),
    node_config=NodeSearchConfig(search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.bfs], reranker=NodeReranker.rrf),
    episode_config=EpisodeSearchConfig(search_methods=[EpisodeSearchMethod.bm25], reranker=EpisodeReranker.rrf),
)

# [label, key] of the record's own node in episode ep's attrs
MAIN_NODE = """(select array[x->>'label', x->>'key'] from jsonb_array_elements(ep.attrs->'nodes') with ordinality t(x, i)
                 where x->>'label' in ('WorkItem', 'Change', 'Message') order by i limit 1)"""

# The graph node of each (source, object_id), from its newest loaded episode
NODES_OF_RECORDS = f"""
select q.source, q.object_id, n.uuid::text as uuid, n.key, n.embed_text, g.name as graph, g.scope, g.source as graph_source
from unnest($1::text[], $2::text[]) q(source, object_id)
cross join lateral (
    select n.* from source_records r
    join episodes ep on ep.source_record_id = r.id
    join graph.nodes n on n.graph_id = ep.graph_id and array[n.label, n.key] = {MAIN_NODE}
    where r.source = q.source and r.object_id = q.object_id and not n.deleted
    order by ep.occurred_at desc, ep.id desc limit 1) n
join graph.graphs g on g.id = n.graph_id
"""

# The source record behind each node or episode uuid. A node maps only when it is some record's own node.
RECORDS_OF = f"""
select u.uuid::text as uuid, r.source, r.object_id
from unnest($1::uuid[]) u(uuid)
cross join lateral (
    (select r.source, r.object_id from episodes ep join source_records r on r.id = ep.source_record_id
      where ep.uuid = u.uuid)
    union all
    (select r.source, r.object_id from graph.nodes n
       join graph.mentions m on m.node_id = n.id
       join episodes ep on ep.id = m.episode_id
       join source_records r on r.id = ep.source_record_id
      where n.uuid = u.uuid and n.label in ('WorkItem', 'Change', 'Message') and array[n.label, n.key] = {MAIN_NODE}
      order by ep.occurred_at desc limit 1)
    limit 1) r
"""

# The nodes the identifiers name, in the order given. ponytail: lower(key) scans the graph's WorkItems and
# Changes (GitHub keys keep the payload's case, scopes are lowercased); store a lowercased key if graphs grow large.
NAMED = """
select n.id, n.uuid::text as uuid from unnest($1::bigint[], $2::text[]) with ordinality q(graph_id, key, i)
join graph.nodes n on n.graph_id = q.graph_id and n.label in ('WorkItem', 'Change') and lower(n.key) = lower(q.key)
where not n.deleted order by q.i
"""

# Nodes whose current REFERENCES edges point at these nodes: Slack messages, comments, PRs naming them
REFERENCING = """
select s.uuid::text as uuid from graph.edges e join graph.nodes s on s.id = e.src
where e.dst = any($1::bigint[]) and e.type = 'REFERENCES' and e.invalid_at is null and e.expired_at is null
  and not s.deleted
order by array_position($1::bigint[], e.dst), e.valid_at
"""

# The episodes that set and ended each fact, with the source record version behind each
PROVENANCE = """
select e.uuid::text as uuid,
       s.key as set_episode, sr.source as set_source, sr.object_id as set_object_id, sr.version as set_version,
       x.key as end_episode, xr.source as end_source, xr.object_id as end_object_id, xr.version as end_version
from graph.edges e
left join episodes s on s.id = e.episode_id left join source_records sr on sr.id = s.source_record_id
left join episodes x on x.id = e.invalidated_by left join source_records xr on xr.id = x.source_record_id
where e.uuid = any($1::uuid[])
"""

_graphiti: Graphiti | None = None


def graphiti() -> Graphiti:
    """One Graphiti on PostgresDriver per process. The driver opens no connection of its own: every call
    runs in reader()'s, through driver.transaction(conn). The model clients are never called (the query
    vector is passed in and the rerankers are RRF), so they get a placeholder key."""
    global _graphiti
    if _graphiti is None:
        unused = "unused-by-ember-mcp"
        _graphiti = Graphiti(
            graph_driver=PostgresDriver(None),
            llm_client=OpenAIClient(LLMConfig(api_key=unused)),
            embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key=unused)),
            cross_encoder=OpenAIRerankerClient(LLMConfig(api_key=unused)),
        )
    return _graphiti


async def search(conn: asyncpg.Connection, question: str, vector: list[float] | None,
                 group_ids: list[str] | None, limit: int) -> list[list[tuple[str, str]]]:
    """Graphiti hybrid search, as ranked lists of (source, object_id): its episodes, its nodes, and the
    endpoints of its facts. group_ids None searches every graph the reader sees, link edges included."""
    config = (COMBINED_HYBRID_SEARCH_RRF if vector is not None else TEXT_AND_TRAVERSAL).model_copy(update={"limit": limit})
    found = await graphiti().search_(question, config=config, group_ids=group_ids, query_vector=vector)
    ranked = [[e.uuid for e in found.episodes], [n.uuid for n in found.nodes],
              [u for e in found.edges for u in (e.source_node_uuid, e.target_node_uuid)]]
    rows = await conn.fetch(RECORDS_OF, list({u for r in ranked for u in r}))
    record = {row["uuid"]: (row["source"], row["object_id"]) for row in rows}
    return [[record[u] for u in r if u in record] for r in ranked]


def identifiers(question: str, graphs: list) -> list[tuple[int, str]]:
    """(graph id, node key) for each identifier the question names that one of these graphs holds: Jira keys
    of the graphs' projects, owner/repo#n of their GitHub repos, and a bare #n when there is one GitHub repo."""
    jira = {}
    for g in graphs:
        if g["source"] == "jira":
            jira.setdefault(g["scope"].rpartition("/")[2], []).append(g["id"])
    github = {g["scope"]: g["id"] for g in graphs if g["source"] == "github"}
    found = [(graph_id, m.group(0)) for m in JIRA_KEY.finditer(question) for graph_id in jira.get(m.group(1), [])]
    found += [(github[m.group(1).lower()], f"{m.group(1)}#{m.group(2)}")
              for m in REPO_NUMBER.finditer(question) if m.group(1).lower() in github]
    if len(github) == 1:
        [(repo, graph_id)] = github.items()
        found += [(graph_id, f"{repo}#{m.group(1)}") for m in BARE_NUMBER.finditer(question)]
    return list(dict.fromkeys(found))


async def named(conn: asyncpg.Connection, names: list[tuple[int, str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The records behind the named nodes, and the records whose nodes reference them, each in order."""
    if not names:
        return [], []
    nodes = await conn.fetch(NAMED, [g for g, _ in names], [k for _, k in names])
    referencing = [r["uuid"] for r in await conn.fetch(REFERENCING, [n["id"] for n in nodes])]
    uuids = [n["uuid"] for n in nodes]
    record = {r["uuid"]: (r["source"], r["object_id"]) for r in await conn.fetch(RECORDS_OF, uuids + referencing)}
    return [record[u] for u in uuids if u in record], [record[u] for u in referencing if u in record]


async def nodes_of(conn: asyncpg.Connection, records: list[tuple[str, str]]) -> dict[tuple[str, str], asyncpg.Record]:
    """Each record's graph node (uuid, key, embed_text, graph, scope, graph_source), for the records that have one."""
    rows = await conn.fetch(NODES_OF_RECORDS, [s for s, _ in records], [o for _, o in records])
    return {(row["source"], row["object_id"]): row for row in rows}


async def facts(node_uuid: str, *, current: bool) -> list[EntityEdge]:
    """Every fact with this node at either end, newest first. current: only those that still hold."""
    edges = await EntityEdge.get_by_node_uuid(graphiti().driver, node_uuid)
    if current:
        edges = [e for e in edges if e.invalid_at is None and e.expired_at is None]
    return sorted(edges, key=lambda e: (e.valid_at is not None, e.valid_at), reverse=True)


def fact(edge: EntityEdge) -> dict:
    return {"type": edge.name, "fact": edge.fact, "valid_at": edge.valid_at and edge.valid_at.isoformat()}


async def provenance(conn: asyncpg.Connection, edges: list[EntityEdge]) -> dict[str, asyncpg.Record]:
    rows = await conn.fetch(PROVENANCE, [e.uuid for e in edges])
    return {row["uuid"]: row for row in rows}
