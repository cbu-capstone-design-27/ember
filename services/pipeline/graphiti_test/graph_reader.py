"""Everything the harness reads straight from the graph, in SQL over Ember's graph tables. Search goes through Graphiti.

The fork maps Graphiti's models onto the tables (graphiti_core/driver/postgres/sql.py): an Entity is a graph.nodes row
whose label is its one declared type and whose props are its attributes, a RELATES_TO edge is a graph.edges row whose
type is its name, an Episodic node is an episodes row, and MENTIONS is graph.mentions. group_id is graph.graphs.group_id.
"""

from __future__ import annotations

import json
from collections import Counter

from ontology import EDGE_TYPE_MAP, ENTITY_TYPES

from .data import record_of

RESEND_SUFFIX = "#resend"  # the resend check's episode; never counted as a record
REQUIRED_SOURCE_REF = ("source", "kind", "external_id")

# The graph ids behind a list of group_ids ($1)
GRAPH_IDS = "(select id from graph.graphs where group_id = any($1::text[]))"


def _iso(value) -> str | None:
    return value.isoformat() if value else None


class GraphReader:
    def __init__(self, driver) -> None:
        self.driver = driver

    async def _rows(self, sql: str, *params) -> list[dict]:
        async with self.driver.connection() as conn:
            return [dict(r) for r in await conn.fetch(sql, *params)]

    async def server(self) -> dict:
        [row] = await self._rows(
            "select 'PostgreSQL' as name, current_setting('server_version') as version, "
            "'pgvector ' || (select extversion from pg_extension where extname = 'vector') as edition")
        return row

    async def indexes(self) -> list[dict]:
        """The graph tables' indexes. The migrations own them; ONLINE means valid and ready."""
        return await self._rows(
            "select c.relname as name, am.amname as type, "
            "case when i.indisvalid and i.indisready then 'ONLINE' else 'NOT ONLINE' end as state "
            "from pg_index i join pg_class c on c.oid = i.indexrelid join pg_am am on am.oid = c.relam "
            "join pg_class t on t.oid = i.indrelid join pg_namespace n on n.oid = t.relnamespace "
            "where n.nspname = 'graph' or (n.nspname = 'public' and t.relname = 'episodes') order by name")

    async def reset(self, groups: list[str]) -> None:
        """Empty the harness's graphs and keep their rows, so group_ids stay the same. Edges and mentions cascade."""
        async with self.driver.connection() as conn:
            await conn.execute(f"delete from graph.nodes where graph_id in {GRAPH_IDS}", groups)
            await conn.execute(f"delete from graph.edges where graph_id in {GRAPH_IDS}", groups)
            await conn.execute(f"delete from episodes where graph_id in {GRAPH_IDS}", groups)

    async def episode_names(self, groups: list[str]) -> set[str]:
        rows = await self._rows(f"select name from episodes where graph_id in {GRAPH_IDS}", groups)
        return {r["name"] for r in rows}

    async def node_names(self, uuids: list[str]) -> dict[str, str]:
        rows = await self._rows("select uuid::text as uuid, name from graph.nodes where uuid = any($1::uuid[])", uuids)
        return {r["uuid"]: r["name"] for r in rows}

    async def records_of_episodes(self, uuids: list[str]) -> dict[str, str]:
        rows = await self._rows("select uuid::text as uuid, name from episodes where uuid = any($1::uuid[])", uuids)
        return {r["uuid"]: record_of(r["name"].removesuffix(RESEND_SUFFIX)) for r in rows}

    async def records_mentioning(self, node_uuids: list[str]) -> dict[str, list[str]]:
        rows = await self._rows(
            "select n.uuid::text as uuid, array_agg(e.name order by e.occurred_at, e.name) as names "
            "from graph.mentions m join graph.nodes n on n.id = m.node_id join episodes e on e.id = m.episode_id "
            "where n.uuid = any($1::uuid[]) group by n.uuid", node_uuids)
        return {r["uuid"]: [record_of(n.removesuffix(RESEND_SUFFIX)) for n in r["names"]] for r in rows}

    async def group_report(self, group: str) -> dict:
        """What landed in one graph, and how well it fits the ontology (ADR 0002 criteria 1 and 5)."""
        graph = "(select id from graph.graphs where group_id = $1)"
        nodes = await self._rows(
            "select array['Entity', label] as labels, array(select jsonb_object_keys(props)) as keys "
            f"from graph.nodes where graph_id = {graph}", group)
        edges = await self._rows(
            "select e.type as name, array['Entity', a.label] as source, array['Entity', b.label] as target "
            "from graph.edges e join graph.nodes a on a.id = e.src join graph.nodes b on b.id = e.dst "
            f"where e.graph_id = {graph}", group)
        [episodes] = await self._rows(
            f"select count(*) as n from episodes where graph_id = {graph} and name not like '%' || $2", group,
            RESEND_SUFFIX)
        [mentions] = await self._rows(
            "select count(*) as n from graph.mentions m join episodes e on e.id = m.episode_id "
            f"where e.graph_id = {graph} and e.name not like '%' || $2", group, RESEND_SUFFIX)

        by_label, missing_source_ref = Counter(), Counter()
        for node in nodes:
            declared = [label for label in node["labels"] if label in ENTITY_TYPES]
            by_label[declared[0] if declared else "Entity (no declared type)"] += 1
            required = declared and "source" in ENTITY_TYPES[declared[0]].model_fields
            if required and any(field not in node["keys"] for field in REQUIRED_SOURCE_REF):
                missing_source_ref[declared[0]] += 1

        by_edge, off_map = Counter(), Counter()
        for edge in edges:
            by_edge[edge["name"]] += 1
            allowed = any(edge["name"] in EDGE_TYPE_MAP.get((s, t), [])
                          for s in edge["source"] for t in edge["target"])
            if not allowed:
                off_map[edge["name"]] += 1
        return {
            "episodes": episodes["n"], "mentions": mentions["n"], "entities": len(nodes), "edges": len(edges),
            "entities_by_type": dict(sorted(by_label.items())),
            "entities_missing_source_ref": dict(sorted(missing_source_ref.items())),
            "edges_by_type": dict(sorted(by_edge.items())),
            "edges_off_the_ontology_map": dict(sorted(off_map.items())),
        }

    async def crossing_edges(self, groups: list[str]) -> int:
        """EMBER-86's subgraph-isolation shape: edges joining two graphs. Must be 0."""
        [row] = await self._rows(
            "select count(*) as n from graph.edges e join graph.nodes a on a.id = e.src "
            f"join graph.nodes b on b.id = e.dst where a.graph_id in {GRAPH_IDS} and a.graph_id <> b.graph_id",
            groups)
        return row["n"]

    async def invalidated(self, groups: list[str]) -> list[dict]:
        """Every fact Graphiti has ended, oldest first."""
        rows = await self._rows(
            "select a.name as source, e.type, b.name as target, e.fact, g.group_id, e.valid_at, e.invalid_at, "
            "coalesce(e.props->'episodes', jsonb_build_array(ep.uuid::text)) as episodes "
            "from graph.edges e join graph.graphs g on g.id = e.graph_id "
            "join graph.nodes a on a.id = e.src join graph.nodes b on b.id = e.dst "
            "left join episodes ep on ep.id = e.episode_id "
            "where g.group_id = any($1::text[]) and (e.invalid_at is not null or e.expired_at is not null) "
            "order by e.valid_at, e.fact", groups)
        for row in rows:
            row["valid_at"], row["invalid_at"] = _iso(row["valid_at"]), _iso(row["invalid_at"])
            episodes = [u for u in json.loads(row.pop("episodes") or "[]") if u]
            names = await self.records_of_episodes(episodes)
            row["records"] = sorted(names.values())
        return rows
