"""Write graph records to Neo4j with plain Cypher. Labels and relationship types
cannot be Cypher parameters, so they are checked against the ontology whitelist
before being placed in a query."""

from __future__ import annotations

from dataclasses import asdict
from itertools import groupby

from ontology import EDGE_TYPES, ENTITY_TYPES
from ontology.config import SCHEMA_VERSION, EmbeddingIdentity

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
    """Source-backed labels merge on the key the uniqueness constraints in schema.cypher cover,
    (group_id, source, external_id), then stamp the uuid, so a node that already exists under a
    different or missing uuid is adopted instead of tripping the constraint. Labels without a
    source (Person) merge on (group_id, uuid)."""
    if _label(label) in SOURCE_BACKED:
        return (f"UNWIND $rows AS row MERGE (n:{label} "
                "{group_id: row.group_id, source: row.source, external_id: row.external_id}) "
                "SET n += row.props SET n.uuid = row.uuid")
    return (f"UNWIND $rows AS row MERGE (n:{label} {{group_id: row.group_id, uuid: row.uuid}}) "
            "SET n += row.props")


def _node_row(node: NodeRec) -> dict:
    row = {"group_id": node.group_id, "uuid": node.uuid, "props": node.props}
    if node.label in SOURCE_BACKED:
        for key in ("source", "external_id"):
            if not node.props.get(key):
                raise ValueError(f"{node.label} {node.key!r} has no {key}; cannot merge on the schema key")
            row[key] = node.props[key]
    return row


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
            rows = [_node_row(n) for n in group]
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

    def read_embedding_identity(self):
        rows = self._run("MATCH (c:EmberConfig {key: 'embedding'}) RETURN c.model AS model, "
                         "c.revision AS revision, c.dimension AS dimension, c.query_template AS query_template")
        return EmbeddingIdentity(**rows[0]) if rows else None

    def write_embedding_identity(self, identity) -> None:
        self._run("MERGE (c:EmberConfig {key: 'embedding'}) ON CREATE SET c.model = $model, "
                  "c.revision = $revision, c.dimension = $dimension, c.query_template = $query_template, "
                  "c.schema_version = $schema_version", schema_version=SCHEMA_VERSION, **asdict(identity))

    def embed_candidates(self, group_ids: list[str]) -> list[dict]:
        found: list[dict] = []
        for label in sorted(SOURCE_BACKED):
            rows = self._run(
                f"MATCH (n:{_label(label)}) WHERE n.group_id IN $gids AND n.embed_text IS NOT NULL "
                "AND trim(n.embed_text) <> '' "
                "RETURN n.uuid AS uuid, n.group_id AS group_id, n.external_id AS key, "
                "n.embed_text AS text, n.embedding_text_hash AS hash, n.embedding AS vector",
                gids=group_ids)
            found += [{"label": label, **row} for row in rows]
        return found

    def write_embeddings(self, rows: list[dict]) -> None:
        for label, group in groupby(sorted(rows, key=lambda r: r["label"]), key=lambda r: r["label"]):
            payload = [{"group_id": r["group_id"], "uuid": r["uuid"], "vector": r["vector"], "hash": r["hash"]}
                       for r in group]
            for chunk in _chunks(payload, 100):
                self._run(f"UNWIND $rows AS row MATCH (n:{_label(label)} {{group_id: row.group_id, uuid: row.uuid}}) "
                          "SET n.embedding = row.vector, n.embedding_text_hash = row.hash", rows=chunk)

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
