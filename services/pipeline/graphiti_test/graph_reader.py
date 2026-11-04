"""Everything the harness reads straight from the graph. All its Cypher is here.

Search goes through Graphiti, so the Postgres fork runs that part unchanged. These reads do not: the fork needs its
own reader with the same methods, returning the same shapes.
"""

from __future__ import annotations

from collections import Counter

from ontology import EDGE_TYPE_MAP, ENTITY_TYPES

from .data import record_of

RESEND_SUFFIX = "#resend"  # the resend check's episode; never counted as a record
REQUIRED_SOURCE_REF = ("source", "kind", "external_id")


def _iso(value) -> str | None:
    return value.iso_format() if hasattr(value, "iso_format") else (value.isoformat() if value else None)


class GraphReader:
    def __init__(self, driver) -> None:
        self.driver = driver

    async def _rows(self, cypher: str, **params) -> list[dict]:
        records, _, _ = await self.driver.execute_query(cypher, params=params)
        return [dict(r) for r in records]

    async def server(self) -> dict:
        [row] = await self._rows("CALL dbms.components() YIELD name, versions, edition "
                                 "RETURN name, versions[0] AS version, edition")
        return row

    async def indexes(self) -> list[dict]:
        rows = await self._rows("SHOW INDEXES YIELD name, type, state RETURN name, type, state ORDER BY name")
        return rows

    async def reset(self, groups: list[str]) -> None:
        await self._rows("MATCH (n) WHERE n.group_id IN $groups DETACH DELETE n", groups=groups)

    async def episode_names(self, groups: list[str]) -> set[str]:
        rows = await self._rows("MATCH (e:Episodic) WHERE e.group_id IN $groups RETURN e.name AS name", groups=groups)
        return {r["name"] for r in rows}

    async def node_names(self, uuids: list[str]) -> dict[str, str]:
        rows = await self._rows("MATCH (n:Entity) WHERE n.uuid IN $uuids RETURN n.uuid AS uuid, n.name AS name",
                                uuids=uuids)
        return {r["uuid"]: r["name"] for r in rows}

    async def records_of_episodes(self, uuids: list[str]) -> dict[str, str]:
        rows = await self._rows("MATCH (e:Episodic) WHERE e.uuid IN $uuids RETURN e.uuid AS uuid, e.name AS name",
                                uuids=uuids)
        return {r["uuid"]: record_of(r["name"].removesuffix(RESEND_SUFFIX)) for r in rows}

    async def records_mentioning(self, node_uuids: list[str]) -> dict[str, list[str]]:
        rows = await self._rows(
            "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE n.uuid IN $uuids "
            "WITH n, e ORDER BY e.valid_at, e.name "
            "RETURN n.uuid AS uuid, collect(e.name) AS names", uuids=node_uuids)
        return {r["uuid"]: [record_of(n.removesuffix(RESEND_SUFFIX)) for n in r["names"]] for r in rows}

    async def group_report(self, group: str) -> dict:
        """What landed in one subgraph, and how well it fits the ontology (ADR 0002 criteria 1 and 5)."""
        nodes = await self._rows("MATCH (n:Entity {group_id: $g}) RETURN labels(n) AS labels, keys(n) AS keys", g=group)
        edges = await self._rows(
            "MATCH (a:Entity)-[r:RELATES_TO {group_id: $g}]->(b:Entity) "
            "RETURN r.name AS name, labels(a) AS source, labels(b) AS target", g=group)
        [episodes] = await self._rows(
            "MATCH (e:Episodic {group_id: $g}) WHERE NOT e.name ENDS WITH $suffix RETURN count(e) AS n",
            g=group, suffix=RESEND_SUFFIX)
        [mentions] = await self._rows(
            "MATCH (e:Episodic {group_id: $g})-[r:MENTIONS]->() WHERE NOT e.name ENDS WITH $suffix RETURN count(r) AS n",
            g=group, suffix=RESEND_SUFFIX)

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
        """EMBER-86's subgraph-isolation shape: edges joining two subgraphs. Must be 0."""
        [row] = await self._rows(
            "MATCH (a)-[r]->(b) WHERE a.group_id IN $groups AND b.group_id IS NOT NULL AND a.group_id <> b.group_id "
            "RETURN count(r) AS n", groups=groups)
        return row["n"]

    async def invalidated(self, groups: list[str]) -> list[dict]:
        """Every fact Graphiti has ended, oldest first."""
        rows = await self._rows(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE r.group_id IN $groups "
            "AND (r.invalid_at IS NOT NULL OR r.expired_at IS NOT NULL) "
            "RETURN a.name AS source, r.name AS type, b.name AS target, r.fact AS fact, r.group_id AS group_id, "
            "r.valid_at AS valid_at, r.invalid_at AS invalid_at, r.episodes AS episodes "
            "ORDER BY r.valid_at, r.fact", groups=groups)
        for row in rows:
            row["valid_at"], row["invalid_at"] = _iso(row["valid_at"]), _iso(row["invalid_at"])
            names = await self.records_of_episodes(row.pop("episodes") or [])
            row["records"] = sorted(names.values())
        return rows
