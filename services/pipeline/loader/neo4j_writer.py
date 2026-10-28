"""Write graph records to Neo4j with plain Cypher. Labels and relationship types
cannot be Cypher parameters, so they are checked against the ontology whitelist
before being placed in a query."""

from __future__ import annotations

import logging
from dataclasses import asdict
from itertools import groupby

from ontology import EDGE_TYPES, ENTITY_TYPES, parse_subgraph_id
from ontology.config import SCHEMA_VERSION, EmbeddingIdentity

from .facts import SCHEMA_PATH, SOURCE_BACKED, GraphFacts, parse_schema_statements
from .records import EdgeRec, NodeRec
from .snapshot import Snapshot, make_ref

CHUNK = 500


def _label(name: str) -> str:
    if name not in ENTITY_TYPES:
        raise ValueError(f"not a node label in the ontology: {name!r}")
    return name


def _rel(name: str) -> str:
    if name not in EDGE_TYPES:
        raise ValueError(f"not a relationship type in the ontology: {name!r}")
    return name


# Applying a row by the source's ordering value (AGENTS.md; EMBER-85). A row whose occurred_at is not
# older than the stored one sets its props. An older row only fills props the element lacks, and
# changes nothing on a deleted node, so a late edit can't undo a newer one or bring back a delete.
# A row or element without occurred_at (the JSONL loader) always applies. Rows reach a query at most
# once per element (_rounds): Cypher runs each clause over every row before the next clause, so two
# rows for one element in one query would both read the state before either wrote.
def _guarded_set(var: str, deletable: bool) -> str:
    fill_ok = "newer OR coalesce(cur.deleted, false) = false" if deletable else "true"
    return (f"WITH {var}, row, properties({var}) AS cur "
            f"WITH {var}, row, cur, (row.occurred_at IS NULL OR cur.occurred_at IS NULL "
            "OR row.occurred_at >= cur.occurred_at) AS newer "
            f"SET {var} += CASE WHEN {fill_ok} THEN row.props ELSE {{}} END "
            f"SET {var} += CASE WHEN newer THEN {{}} ELSE cur END "
            f"SET {var}.occurred_at = CASE WHEN newer THEN coalesce(row.occurred_at, cur.occurred_at) "
            "ELSE cur.occurred_at END ")


def node_query(label: str) -> str:
    """Source-backed labels merge on the key the uniqueness constraints in schema.cypher cover,
    (group_id, source, external_id), then stamp the uuid, so a node that already exists under a
    different or missing uuid is adopted instead of tripping the constraint. Labels without a
    source (Person) merge on (group_id, uuid). Props apply by occurred_at (_guarded_set), and a
    placeholder an edge created (edge_query) becomes a real node."""
    if _label(label) in SOURCE_BACKED:
        return (f"UNWIND $rows AS row MERGE (n:{label} "
                "{group_id: row.group_id, source: row.source, external_id: row.external_id}) "
                + _guarded_set("n", deletable=True) + "SET n.uuid = row.uuid REMOVE n.placeholder")
    return (f"UNWIND $rows AS row MERGE (n:{label} {{group_id: row.group_id, uuid: row.uuid}}) "
            + _guarded_set("n", deletable=True) + "REMOVE n.placeholder")


def _node_row(node: NodeRec) -> dict:
    row = {"group_id": node.group_id, "uuid": node.uuid, "props": node.props}
    if node.label in SOURCE_BACKED:
        for key in ("source", "external_id"):
            if not node.props.get(key):
                raise ValueError(f"{node.label} {node.key!r} has no {key}; cannot merge on the schema key")
            row[key] = node.props[key]
    if node.occurred_at is not None:
        row["occurred_at"] = node.occurred_at
    return row


def _endpoint(var: str, label: str, end: str, placeholders: bool) -> str:
    """MATCH an edge's endpoint by uuid. With placeholders, a source-backed endpoint that is not in
    the graph yet is created as a bare node (placeholder: true) on its schema key, which the node's
    own record fills in when it loads: the Postgres loader loads one subgraph in batches, so an edge
    can arrive before the node it points to (a Jira comment before its issue's latest version)."""
    if placeholders and _label(label) in SOURCE_BACKED:
        return (f"MERGE ({var}:{label} {{group_id: row.group_id, source: row.source, external_id: row.{end}_key}}) "
                f"ON CREATE SET {var}.uuid = row.{end}, {var}.placeholder = true ")
    return f"MATCH ({var}:{_label(label)} {{group_id: row.group_id, uuid: row.{end}}}) "


def edge_query(rel: str, src: str, dst: str, placeholders: bool = False) -> str:
    return ("UNWIND $rows AS row "
            # WITH between the endpoints: Cypher needs one between a MERGE and a following MATCH
            + _endpoint("a", src, "src", placeholders) + "WITH row, a "
            + _endpoint("b", dst, "dst", placeholders) + "WITH row, a, b "
            + f"MERGE (a)-[r:{_rel(rel)}]->(b) SET r.group_id = row.group_id "
            + _guarded_set("r", deletable=False) + "RETURN count(r) AS n")


def _edge_row(edge: EdgeRec, placeholders: bool) -> dict:
    row = {"group_id": edge.group_id, "src": edge.src_uuid, "dst": edge.dst_uuid, "props": edge.props}
    if placeholders:
        row.update(source=parse_subgraph_id(edge.group_id)[1], src_key=edge.src_key, dst_key=edge.dst_key)
    if edge.occurred_at is not None:
        row["occurred_at"] = edge.occurred_at
    return row


def _rounds(rows: list[dict], key) -> list[list[dict]]:
    """Split rows so each element appears at most once per round, keeping each element's rows in order."""
    rounds: list[list[dict]] = []
    seen: dict = {}
    for row in rows:
        index = seen.get(key(row), 0)
        seen[key(row)] = index + 1
        if index == len(rounds):
            rounds.append([])
        rounds[index].append(row)
    return rounds


def _chunks(items: list, size: int = CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str, database: str | None = None, driver=None) -> None:
        if driver is None:
            from neo4j import GraphDatabase  # imported here so unit tests need no driver

            # A first run asks for properties that do not exist yet; that is expected, not a problem.
            logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
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
        """Rows for one node apply in the order given (sorted() is stable)."""
        for label, group in groupby(sorted(nodes, key=lambda n: n.label), key=lambda n: n.label):
            rows = [_node_row(n) for n in group]
            for round_ in _rounds(rows, key=lambda r: (r["group_id"], r["uuid"])):
                for chunk in _chunks(round_):
                    self._run(node_query(label), rows=chunk)
        return len(nodes)

    def write_edges(self, edges: list[EdgeRec], placeholders: bool = False) -> int:
        """Returns how many rows matched both endpoints. With placeholders, a missing source-backed
        endpoint is created bare instead of dropping the edge (_endpoint)."""
        def kind(e: EdgeRec):
            return (e.type, e.src_label, e.dst_label)

        written = 0
        for (rel, src, dst), group in groupby(sorted(edges, key=kind), key=kind):
            rows = [_edge_row(e, placeholders) for e in group]
            for round_ in _rounds(rows, key=lambda r: (r["group_id"], r["src"], r["dst"])):
                for chunk in _chunks(round_):
                    result = self._run(edge_query(rel, src, dst, placeholders), rows=chunk)
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

    def snapshot(self, group_ids: list[str]) -> Snapshot:
        snap = Snapshot()
        for row in self._run("MATCH (n) WHERE n.group_id IN $gids "
                             "AND n.external_id IS NOT NULL AND n.external_id <> '' "
                             "AND n.source IS NOT NULL AND n.source <> '' "
                             "RETURN labels(n)[0] AS label, n {.*, embedding: null, embedding_text_hash: null} AS props",
                             gids=group_ids):
            props = {k: v for k, v in row["props"].items() if v is not None}
            snap.nodes.append({"label": row["label"], "ref": make_ref(props["source"], props["external_id"]),
                               "source": props["source"], "props": props})
        for row in self._run("MATCH (a)-[r]->(b) WHERE a.group_id IN $gids AND b.group_id IN $gids "
                             "AND r.group_id IN $gids "
                             "AND a.external_id IS NOT NULL AND a.external_id <> '' "
                             "AND a.source IS NOT NULL AND a.source <> '' "
                             "AND b.external_id IS NOT NULL AND b.external_id <> '' "
                             "AND b.source IS NOT NULL AND b.source <> '' "
                             "RETURN type(r) AS type, labels(a)[0] AS src_label, a.source AS src_source, "
                             "a.external_id AS src_id, labels(b)[0] AS dst_label, b.source AS dst_source, "
                             "b.external_id AS dst_id", gids=group_ids):
            snap.edges.append({"type": row["type"], "src_label": row["src_label"],
                               "src": make_ref(row["src_source"], row["src_id"]),
                               "dst_label": row["dst_label"],
                               "dst": make_ref(row["dst_source"], row["dst_id"])})
        return snap
