"""Scratch benchmark for docs/research/postgres-graph.md (EMBER-117): the same graph in plain Postgres tables,
Apache AGE and Neo4j, timed on four representative reads.

The graph is the photon data as extract maps it (episodes.attrs, EMBER-85), copied once per tenant: --scale 100
gives 100 tenants of photon size. Each Message gets a random 1024-dim vector (ADR 0001's dimension), the same
vectors in every engine. Run against scratch containers only; it drops and recreates schema `bench`, AGE graph
`bench_age` and every node in the Neo4j database it is given.

    PG_URL=postgresql://postgres:pw@localhost:55440/ember NEO4J_URI=bolt://localhost:57687 NEO4J_PASSWORD=... \
        python docs/research/postgres-graph-bench.py --scale 100

PG_URL's database must already hold photon episodes (apps/mcp/seed_photon.py --no-embed, then loader extract)
and have the age and vector extensions.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import random
import statistics
import subprocess
import time

import numpy as np
import psycopg
from neo4j import GraphDatabase

DIM = 1024
RUNS = 30


def photon(pg) -> tuple[list[tuple], list[tuple]]:
    """Distinct (source, label, key) nodes and (source, type, src_label, src_key, dst_label, dst_key) edges."""
    nodes = pg.execute("""select distinct source, n->>'label', n->>'key' from episodes,
                          jsonb_array_elements(attrs->'nodes') n where tenant = 'photon'""").fetchall()
    edges = pg.execute("""select distinct source, e->>'type', e->>'src_label', e->>'src_key', e->>'dst_label', e->>'dst_key'
                          from episodes, jsonb_array_elements(attrs->'edges') e where tenant = 'photon'""").fetchall()
    return nodes, edges


def build(nodes, edges, scale: int):
    """Node rows (id, tenant, source, label, key) and edge rows (src id, dst id, type, tenant, source)."""
    node_rows, edge_rows = [], []
    for t in range(scale):
        tenant = f"t{t}"
        ids = {}
        for source, label, key in nodes:
            ids[(source, label, key)] = len(node_rows) + 1
            node_rows.append((len(node_rows) + 1, tenant, source, label, key))
        for source, typ, sl, sk, dl, dk in edges:
            a, b = ids.get((source, sl, sk)), ids.get((source, dl, dk))
            if a and b:
                edge_rows.append((a, b, typ, tenant, source))
    return node_rows, edge_rows


def vectors(node_rows) -> dict[int, np.ndarray]:
    rng = np.random.default_rng(7)
    out = {}
    for row in node_rows:
        if row[3] == "Message":
            v = rng.standard_normal(DIM).astype(np.float32)
            out[row[0]] = v / np.linalg.norm(v)
    return out


def vec_text(v) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def load_pg(pg, node_rows, edge_rows, vecs):
    pg.execute("drop schema if exists bench cascade; create schema bench")
    pg.execute("""create table bench.nodes (id bigint primary key, tenant text not null, source text not null,
                  label text not null, key text not null, embedding vector(1024), unique (tenant, source, label, key))""")
    pg.execute("""create table bench.edges (src bigint not null, dst bigint not null, type text not null,
                  tenant text not null, source text not null, valid_at timestamptz not null default '2026-01-01',
                  invalid_at timestamptz)""")
    with pg.cursor().copy("copy bench.nodes (id, tenant, source, label, key, embedding) from stdin") as cp:
        for r in node_rows:
            cp.write_row((*r, vec_text(vecs[r[0]]) if r[0] in vecs else None))
    with pg.cursor().copy("copy bench.edges (src, dst, type, tenant, source) from stdin") as cp:
        for r in edge_rows:
            cp.write_row(r)
    pg.execute("create index on bench.edges (src, type) include (dst)")
    pg.execute("create index on bench.edges (dst, type) include (src)")
    pg.execute("create index on bench.nodes using hnsw (embedding vector_cosine_ops) where embedding is not null")
    pg.execute("analyze bench.nodes; analyze bench.edges")


def load_age(pg, node_rows, edge_rows, container: str):
    """Through AGE's CSV loaders: one vertex label table per node label, one edge label table per type."""
    pg.execute("load 'age'; set search_path = ag_catalog, \"$user\", public")
    if pg.execute("select 1 from ag_catalog.ag_graph where name = 'bench_age'").fetchone():
        pg.execute("select drop_graph('bench_age', true)")
    pg.execute("select create_graph('bench_age')")
    labels = {r[3] for r in node_rows}
    for label in sorted(labels):
        buf = io.StringIO("id,group_id,key\n")
        buf.seek(0, 2)
        for r in node_rows:
            if r[3] == label:
                buf.write(f'{r[0]},{r[1]}_{r[2]},"{r[4]}"\n')
        _copy_in(container, f"/tmp/age/v_{label}.csv", buf.getvalue())
        pg.execute(f"select create_vlabel('bench_age', '{label}')")
        pg.execute(f"select load_labels_from_file('bench_age', '{label}', 'v_{label}.csv')")
        pg.execute(f'create index on bench_age."{label}" using gin (properties)')
    label_of = {r[0]: r[3] for r in node_rows}
    for typ in sorted({r[2] for r in edge_rows}):
        buf = io.StringIO()
        buf.write("start_id,start_vertex_type,end_id,end_vertex_type\n")
        for a, b, t, _, _ in edge_rows:
            if t == typ:
                buf.write(f"{a},{label_of[a]},{b},{label_of[b]}\n")
        _copy_in(container, f"/tmp/age/e_{typ}.csv", buf.getvalue())
        pg.execute(f"select create_elabel('bench_age', '{typ}')")
        pg.execute(f"select load_edges_from_file('bench_age', '{typ}', 'e_{typ}.csv')")
    pg.execute("analyze")


def _copy_in(container: str, path: str, text: str):
    subprocess.run(["docker", "exec", "-i", container, "sh", "-c", f"mkdir -p /tmp/age && cat > {path}"], input=text.encode(), check=True)


def load_neo4j(drv, node_rows, edge_rows, vecs):
    with drv.session() as s:
        s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        for (name,) in s.run("SHOW CONSTRAINTS YIELD name RETURN name").values():
            s.run(f"DROP CONSTRAINT {name}").consume()
        for (name,) in s.run("SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name").values():
            s.run(f"DROP INDEX {name}").consume()
        s.run("CREATE INDEX n_nid IF NOT EXISTS FOR (n:N) ON (n.nid)").consume()
        by_label: dict[str, list] = {}
        for r in node_rows:
            row = {"nid": r[0], "group_id": f"{r[1]}_{r[2]}", "key": r[4]}
            if r[0] in vecs:
                row["embedding"] = vecs[r[0]].tolist()
            by_label.setdefault(r[3], []).append(row)
        for label, rows in by_label.items():
            for i in range(0, len(rows), 2000):
                s.run(f"UNWIND $rows AS row CREATE (n:N:{label}) SET n = row", rows=rows[i:i + 2000]).consume()
            s.run(f"CREATE INDEX {label.lower()}_gk IF NOT EXISTS FOR (n:{label}) ON (n.group_id, n.key)").consume()
        s.run("CALL db.awaitIndexes(600)").consume()
        by_type: dict[str, list] = {}
        for a, b, t, _, _ in edge_rows:
            by_type.setdefault(t, []).append({"a": a, "b": b})
        for typ, rows in by_type.items():
            for i in range(0, len(rows), 5000):
                s.run(f"UNWIND $rows AS row MATCH (a:N {{nid: row.a}}), (b:N {{nid: row.b}}) "
                      f"CREATE (a)-[:{typ} {{valid_at: datetime('2026-01-01')}}]->(b)", rows=rows[i:i + 5000]).consume()
        s.run("CREATE VECTOR INDEX msg_embedding IF NOT EXISTS FOR (m:Message) ON m.embedding "
              "OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}}").consume()
        s.run("CALL db.awaitIndexes(1200)").consume()


# Each query takes a tenant and start keys picked at random per run.
PG_Q = {
    "1-hop neighbours of a ticket": """
        select count(*) from bench.nodes w
        join lateral (select dst from bench.edges where src = w.id and invalid_at is null
                      union all select src from bench.edges where dst = w.id and invalid_at is null) n on true
        where w.tenant = %(t)s and w.source = 'jira' and w.label = 'WorkItem' and w.key = %(wi)s""",
    "co-participants of a person (4 hops)": """
        select count(distinct p2.src) from bench.nodes p
        join bench.edges a1 on a1.src = p.id and a1.type = 'AUTHORED'
        join bench.edges pt on pt.src = a1.dst and pt.type = 'PART_OF'
        join bench.edges pt2 on pt2.dst = pt.dst and pt2.type = 'PART_OF'
        join bench.edges p2 on p2.dst = pt2.src and p2.type = 'AUTHORED'
        where p.tenant = %(t)s and p.source = 'slack' and p.label = 'Person' and p.key = %(person)s and p2.src <> p.id""",
    "everything within 3 hops of a ticket": """
        with recursive walk(id, depth) as (
          select id, 0 from bench.nodes where tenant = %(t)s and source = 'jira' and label = 'WorkItem' and key = %(wi)s
          union
          select n.id, w.depth + 1 from walk w
          join lateral (select dst as id from bench.edges where src = w.id
                        union all select src from bench.edges where dst = w.id) n on true
          where w.depth < 3)
        select count(distinct id) - 1 from walk""",
    "vector top-10 messages + thread and author": """
        with top as (select id from bench.nodes where tenant = %(t)s and label = 'Message' and embedding is not null
                     order by embedding <=> %(vec)s::vector limit 10)
        select top.id, c.key, au.src from top
        join bench.edges pt on pt.src = top.id and pt.type = 'PART_OF'
        join bench.nodes c on c.id = pt.dst
        left join bench.edges au on au.dst = top.id and au.type = 'AUTHORED'""",
}

AGE_Q = {
    "1-hop neighbours of a ticket":
        "MATCH (w:WorkItem {{group_id: '{t}_jira', key: '{wi}'}})-[]-(n) RETURN count(n)",
    "co-participants of a person (4 hops)":
        "MATCH (p:Person {{group_id: '{t}_slack', key: '{person}'}})-[:AUTHORED]->(:Message)-[:PART_OF]->(:Conversation)"
        "<-[:PART_OF]-(:Message)<-[:AUTHORED]-(o:Person) WHERE o <> p RETURN count(DISTINCT o)",
    "everything within 3 hops of a ticket":
        "MATCH (w:WorkItem {{group_id: '{t}_jira', key: '{wi}'}})-[*1..3]-(n) RETURN count(DISTINCT n)",
}

NEO_Q = {
    "1-hop neighbours of a ticket":
        "MATCH (w:WorkItem {group_id: $t + '_jira', key: $wi})-[r]-(n) WHERE r.invalid_at IS NULL RETURN count(n)",
    "co-participants of a person (4 hops)":
        "MATCH (p:Person {group_id: $t + '_slack', key: $person})-[:AUTHORED]->(:Message)-[:PART_OF]->(:Conversation)"
        "<-[:PART_OF]-(:Message)<-[:AUTHORED]-(o:Person) WHERE o <> p RETURN count(DISTINCT o)",
    "everything within 3 hops of a ticket":
        "MATCH (w:WorkItem {group_id: $t + '_jira', key: $wi})-[*1..3]-(n) RETURN count(DISTINCT n)",
    # 5.26 Community has no filtered vector search: over-fetch, then keep this tenant's hits
    "vector top-10 messages + thread and author":
        "CALL db.index.vector.queryNodes('msg_embedding', $over, $vec) YIELD node WHERE node.group_id STARTS WITH $t + '_' "
        "WITH node LIMIT 10 MATCH (node)-[:PART_OF]->(c) OPTIONAL MATCH (a)-[:AUTHORED]->(node) RETURN node.nid, c.key, a.nid",
}


def timed(fn, answers: list) -> list[float]:
    answers.append(fn())  # warm-up; its answer is printed so the engines can be compared
    out = []
    for _ in range(RUNS):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def summary(ms: list[float]) -> str:
    ms = sorted(ms)
    return f"{statistics.median(ms):.1f} / {ms[int(len(ms) * 0.95) - 1]:.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--container", default="ember-scratch-pg")
    args = ap.parse_args()
    pg = psycopg.connect(os.environ["PG_URL"], autocommit=True)
    drv = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=("neo4j", os.environ["NEO4J_PASSWORD"]))

    nodes, edges = photon(pg)
    node_rows, edge_rows = build(nodes, edges, args.scale)
    vecs = vectors(node_rows)
    print(f"scale {args.scale}: {len(node_rows)} nodes, {len(edge_rows)} edges, {len(vecs)} vectors")
    loads = {}
    for name, fn in (("postgres", lambda: load_pg(pg, node_rows, edge_rows, vecs)),
                     ("age", lambda: load_age(pg, node_rows, edge_rows, args.container)),
                     ("neo4j", lambda: load_neo4j(drv, node_rows, edge_rows, vecs))):
        start = time.perf_counter()
        fn()
        loads[name] = time.perf_counter() - start
    print("load seconds:", {k: round(v, 1) for k, v in loads.items()})

    wis = [k for s, l, k in nodes if l == "WorkItem"]
    people = [k for s, l, k in nodes if l == "Person" and s == "slack"]
    msg_ids = list(vecs)
    rnd = random.Random(1)

    def params():
        mid = rnd.choice(msg_ids)
        t = f"t{(mid - 1) // len(nodes)}"
        return {"t": t, "wi": rnd.choice(wis), "person": rnd.choice(people), "vec": vecs[mid]}

    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    pg.execute("set hnsw.iterative_scan = strict_order")
    neo = drv.session()
    rows = []
    for q in PG_Q:
        p = params()
        answers: list = []
        res = {"postgres": summary(timed(lambda: pg.execute(PG_Q[q], {**p, "vec": vec_text(p["vec"])}).fetchall(), answers))}
        if q in AGE_Q:
            age_sql = "select * from cypher('bench_age', $$ " + AGE_Q[q].format(**p) + " $$) as (n agtype)"
            pg.execute("set search_path = ag_catalog, \"$user\", public")
            res["age"] = summary(timed(lambda: pg.execute(age_sql).fetchall(), answers))
            pg.execute("set search_path = \"$user\", public")
        else:
            res["age"] = "n/a"
        res["neo4j"] = summary(timed(lambda: neo.run(NEO_Q[q], t=p["t"], wi=p["wi"], person=p["person"],
                                                     vec=p["vec"].tolist(), over=10 * args.scale + 100).data(), answers))
        print(q, "answers:", [a if len(a) == 1 else f"{len(a)} rows" for a in answers])
        rows.append((q, res))
    print(f"\n| Query (scale {args.scale}, median / p95 ms) | Postgres tables | AGE 1.8 | Neo4j 5.26 |\n|---|---|---|---|")
    for q, r in rows:
        print(f"| {q} | {r['postgres']} | {r['age']} | {r['neo4j']} |")
    neo.close()
    drv.close()


if __name__ == "__main__":
    main()
