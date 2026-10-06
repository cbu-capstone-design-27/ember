"""Write graph records to Neo4j with plain Cypher. Labels and relationship types
cannot be Cypher parameters, so they are checked against the ontology whitelist
before being placed in a query."""

from __future__ import annotations

from itertools import groupby

from ontology import EDGE_TYPES, ENTITY_TYPES

from .facts import SCHEMA_PATH, SOURCE_BACKED, GraphFacts, parse_schema_statements
from .records import EdgeRec, NodeRec

CHUNK = 500


def _label(name: str) -> str:
    if name not in ENTITY_TYPES:
        raise ValueError(f"not a node label in the ontology: {name!r}")
    return name


def _rel(name: str) -> str:
    if name not in EDGE_TYPES:
        raise ValueError(f"not a relationship type in the ontology: {name!r}")
    return name


def node_query(label: str) -> str:
    return (f"UNWIND $rows AS row MERGE (n:{_label(label)} {{group_id: row.group_id, uuid: row.uuid}}) "
            "SET n += row.props")


def edge_query(rel: str, src: str, dst: str) -> str:
    return (f"UNWIND $rows AS row "
            f"MATCH (a:{_label(src)} {{group_id: row.group_id, uuid: row.src}}) "
            f"MATCH (b:{_label(dst)} {{group_id: row.group_id, uuid: row.dst}}) "
            f"MERGE (a)-[r:{_rel(rel)}]->(b) SET r.group_id = row.group_id SET r += row.props "
            "RETURN count(r) AS n")


def _chunks(items: list, size: int = CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str, database: str | None = None, driver=None) -> None:
        if driver is None:
            from neo4j import GraphDatabase  # imported here so unit tests need no driver

            driver = GraphDatabase.driver(uri, auth=(user, password))
        self._driver = driver
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def _run(self, query: str, **params) -> list[dict]:
        with self._driver.session(database=self._database) as session:
            return session.run(query, **params).data()

    def ensure_schema(self) -> None:
        for statement in parse_schema_statements(SCHEMA_PATH.read_text(encoding="utf-8")):
            self._run(statement)
        for label in ENTITY_TYPES:
            self._run(f"CREATE INDEX node_lookup_{label.lower()} IF NOT EXISTS "
                      f"FOR (n:{label}) ON (n.group_id, n.uuid)")

    def reset(self, group_ids: list[str]) -> None:
        self._run("MATCH (n) WHERE n.group_id IN $gids DETACH DELETE n", gids=group_ids)

    def write_nodes(self, nodes: list[NodeRec]) -> int:
        for label, group in groupby(sorted(nodes, key=lambda n: n.label), key=lambda n: n.label):
            rows = [{"group_id": n.group_id, "uuid": n.uuid, "props": n.props} for n in group]
            for chunk in _chunks(rows):
                self._run(node_query(label), rows=chunk)
        return len(nodes)

    def write_edges(self, edges: list[EdgeRec]) -> int:
        def kind(e: EdgeRec):
            return (e.type, e.src_label, e.dst_label)

        written = 0
        for (rel, src, dst), group in groupby(sorted(edges, key=kind), key=kind):
            rows = [{"group_id": e.group_id, "src": e.src_uuid, "dst": e.dst_uuid, "props": e.props}
                    for e in group]
            for chunk in _chunks(rows):
                result = self._run(edge_query(rel, src, dst), rows=chunk)
                written += result[0]["n"] if result else 0
        return written

    def read_facts(self, group_ids: list[str]) -> GraphFacts:
        facts = GraphFacts()
        for row in self._run("MATCH (n) WHERE n.group_id IN $gids UNWIND labels(n) AS l "
                             "RETURN l, count(*) AS c", gids=group_ids):
            facts.labels.add(row["l"])
            facts.node_counts[row["l"]] = row["c"]
        for row in self._run("MATCH (a)-[r]->(b) WHERE a.group_id IN $gids "
                             "RETURN labels(a)[0] AS s, type(r) AS t, labels(b)[0] AS d, count(*) AS c",
                             gids=group_ids):
            facts.relationships[(row["s"], row["t"], row["d"])] = row["c"]
        facts.constraints = {row["name"] for row in self._run("SHOW CONSTRAINTS YIELD name RETURN name")}
        for label in sorted(SOURCE_BACKED):
            rows = self._run(f"MATCH (n:{label}) WHERE n.group_id IN $gids AND "
                             "(n.source IS NULL OR n.kind IS NULL OR n.external_id IS NULL) "
                             "RETURN count(n) AS c", gids=group_ids)
            if rows and rows[0]["c"]:
                facts.missing_required[label] = rows[0]["c"]
        return facts
