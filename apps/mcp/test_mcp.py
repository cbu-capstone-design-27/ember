"""ember_mcp against a real Postgres with pgvector and a stub embedding server (EMBER-79, EMBER-78), and its
graph backends (EMBER-122): pggraph on the research prototype's tables, Neo4j through a stub driver call, or a
real Neo4j when NEO4J_TEST_URI and NEO4J_PASSWORD are set.

Needs psql, dbmate, the packages in requirements.txt and services/embed/requirements.txt (the photon
seed embeds through the embed worker), and a Postgres server with the vector extension
it may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 apps/mcp/test_mcp.py

It builds the repo migrations in database ember_test_mcp.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import unittest
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

import asyncpg
from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import seed_photon  # noqa: E402
from ember_mcp import graph, retrieval, server  # noqa: E402

PROTOTYPE = REPO / "docs" / "research" / "postgres-graph-prototype.py"
MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_mcp"
ISSUE_SELF = "https://photon.atlassian.net/rest/api/2/issue/10004"


def database_url() -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def jira_issue(summary: str, description: str) -> dict:
    return {"webhookEvent": "jira:issue_updated",
            "issue": {"id": "10004", "key": "PHO-4", "self": ISSUE_SELF,
                      "fields": {"summary": summary, "description": description}}}


def stub_vector(text: str, dimension: int = 1024) -> list[float]:
    """Hashed bag of words, L2-normalized, so texts sharing words are near each other by cosine.
    A query's instruction prefix is dropped first, so queries and documents hash alike."""
    vector = [0.0] * dimension
    for word in re.findall(r"\w+", text.split("\nQuery: ", 1)[-1].lower()):
        vector[zlib.crc32(word.encode()) % dimension] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


@contextlib.contextmanager
def search_settings(**settings):
    """Connection-local planner controls for tests, including connections opened by search()."""
    original_connect = retrieval.connect

    async def configured_connect():
        conn = await original_connect()
        try:
            for name, value in settings.items():
                await conn.execute("select set_config($1, $2, false)", name, value)
        except BaseException:
            await conn.close()
            raise
        return conn

    with patch.object(retrieval, "connect", configured_connect):
        yield


class StubEmbeddings:
    """POST /v1/embeddings with stub_vector. Records each request's input and Authorization header;
    `dimension` sets the vector length."""

    def __init__(self) -> None:
        self.dimension = 1024
        self.requests: list[list[str]] = []
        self.auth: list[str | None] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == "/v1/embeddings"
                stub.requests.append(body["input"])
                stub.auth.append(self.headers.get("Authorization"))
                data = [{"index": i, "embedding": stub_vector(text, stub.dimension)} for i, text in enumerate(body["input"])]
                payload = json.dumps({"data": data, "model": body["model"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


QUESTION = "Why does the global hotkey open Spotlight instead of Photon?"
PULL, SLACK, COMMIT = "acme/photon#43", "C08DEV/1789353420.000100", "acme/photon@" + "a" * 40
DELETED, UNEMBEDDED = "acme/photon#7", "10005"
ISSUE_TEXT = ("PHO-4: Global hotkey conflicts with Spotlight\n\n"
              "Pressing the global hotkey opens Spotlight instead of Photon. The hotkey conflict needs a new default.")

# (source, kind, object_id, version, occurred_at day, record, embed_text), in the shapes the enrich
# workers store and with the text the embed worker would build. embed_text None: not embedded yet.
ROWS = [
    ("github", "pull_request", PULL, "2026-09-24T10:00:00Z", 24, {"payload": {
        "action": "opened", "pull_request": {
            "number": 43, "title": "Cache app icons on first launch",
            "body": "Icons load from disk once and stay in memory.",
            "html_url": "https://github.com/acme/photon/pull/43"}}},
     "Cache app icons on first launch\n\nIcons load from disk once and stay in memory."),
    ("jira", "issue", "10004", "v1", 20, {"payload": jira_issue("Hotkey bug", "Something is off.")},
     "PHO-4: Hotkey bug\n\nSomething is off."),
    ("jira", "issue", "10004", "v2", 22, {"payload": jira_issue(
        "Global hotkey conflicts with Spotlight",
        "Pressing the global hotkey opens Spotlight instead of Photon. The hotkey conflict needs a new default.")},
     ISSUE_TEXT),
    ("slack", "message", SLACK, "1789353420.000100", 23, {"payload": {
        "channel": "C08DEV", "ts": "1789353420.000100", "user": "U1",
        "text": "Should we ship the icon cache before the release?"}},
     "Should we ship the icon cache before the release?"),
    ("github", "commit", COMMIT, "a" * 40, 25, {"payload": {
        "id": "a" * 40, "message": "Fix the default hotkey conflict with Spotlight\n\nSee PHO-4.",
        "url": "https://github.com/acme/photon/commit/" + "a" * 40}},
     "Fix the default hotkey conflict with Spotlight\n\nSee PHO-4."),
    # An issue whose newest version is a delete: neither version may be returned
    ("github", "issue", DELETED, "v1", 21, {"payload": {"issue": {
        "number": 7, "title": "Global hotkey opens Spotlight instead of Photon"}}},
     "Global hotkey opens Spotlight instead of Photon"),
    ("github", "issue", DELETED, "deleted:1", 26, {"tombstone": True}, '{"tombstone":true}'),
    ("jira", "issue", UNEMBEDDED, "v1", 26, {"payload": jira_issue("Global hotkey opens Spotlight", QUESTION)}, None),
]


class Database(unittest.IsolatedAsyncioTestCase):
    """A migrated ember_test_mcp with ROWS inserted before each test, and the stub embedding server."""

    @classmethod
    def setUpClass(cls):
        run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
            "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
        run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")
        cls.stub = StubEmbeddings()
        os.environ.update(DATABASE_URL=database_url(), EMBEDDING_URL=cls.stub.url, EMBEDDING_API_KEY="test-key")

    @classmethod
    def tearDownClass(cls):
        cls.stub.server.shutdown()
        cls.stub.server.server_close()
        run("psql", "-X", "-q", "-d", os.environ["PGTEST_URL"], "-c", f"drop database if exists {DB} with (force)")

    async def asyncSetUp(self):
        self.stub.dimension = 1024
        self.stub.requests.clear()
        self.stub.auth.clear()
        db = await asyncpg.connect(database_url())
        await db.execute("truncate source_records, episodes restart identity")
        await db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record, embed_text, embedding)"
            " values ($1, 'acme', $2, $3, $4, $5, $6, $7, $8::text::vector)",
            [(source, kind, object_id, version, datetime(2026, 9, day, tzinfo=timezone.utc), json.dumps(record),
              text, None if text is None else "[" + ",".join(map(str, stub_vector(text))) + "]")
             for source, kind, object_id, version, day, record, text in ROWS])
        await db.close()



class McpTest(Database):
    async def test_search_ranks_the_relevant_record_first_and_filters_by_source(self):
        hits = await retrieval.search(QUESTION, k=50)
        self.assertEqual((hits[0]["source"], hits[0]["object_id"], hits[0]["version"]), ("jira", "10004", "v2"))
        # Newest version only; the deleted issue and the unembedded one never appear
        self.assertCountEqual([h["object_id"] for h in hits], ["10004", PULL, SLACK, COMMIT])
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(0 < scores[0] <= 1)

        github = await retrieval.search(QUESTION, sources=["github"])
        self.assertEqual([h["object_id"] for h in github], [COMMIT, PULL])
        icons = await retrieval.search("Are we caching the icons before release?", sources=["slack", "jira"])
        self.assertEqual([h["source"] for h in icons], ["slack", "jira"])

    async def test_unembedded_rows_are_never_returned(self):
        hits = await retrieval.search("Global hotkey opens Spotlight " + QUESTION, k=50, sources=["jira"])
        self.assertEqual([h["object_id"] for h in hits], ["10004"])

    async def test_hnsw_index_returns_the_controlled_nearest_record(self):
        """Exercise the approximate index explicitly, on a corpus smaller than its search budget."""
        vector = "[" + ",".join(map(str, stub_vector(ISSUE_TEXT))) + "]"
        query = """select object_id, version from source_records where embedding is not null
                   order by embedding <=> $1::text::vector limit 3"""
        with search_settings(enable_seqscan="off"):
            conn = await retrieval.connect()
            try:
                plan = await conn.fetchval("explain (format json) " + query, vector)
                self.assertIn("source_records_embedding", plan)
                hits = await conn.fetch(query, vector)
            finally:
                await conn.close()
        self.assertEqual((hits[0]["object_id"], hits[0]["version"]), ("10004", "v2"))

    async def test_hits_carry_url_and_snippet_and_get_record_returns_the_newest_version(self):
        issue, commit = await retrieval.search(QUESTION, k=2)
        self.assertEqual(issue["url"], "https://photon.atlassian.net/browse/PHO-4")
        self.assertEqual(issue["title"], "PHO-4 Global hotkey conflicts with Spotlight")
        self.assertEqual(issue["snippet"], ISSUE_TEXT)
        self.assertEqual(commit["url"], "https://github.com/acme/photon/commit/" + "a" * 40)
        self.assertEqual(commit["title"], "Fix the default hotkey conflict with Spotlight")
        [pull] = await retrieval.search("Do icons stay in memory?", k=1, sources=["github"])
        self.assertEqual(pull["url"], "https://github.com/acme/photon/pull/43")

        record = await retrieval.get_record("jira", "10004")
        self.assertEqual(record["version"], "v2")
        self.assertEqual(record["record"]["payload"]["issue"]["fields"]["summary"], "Global hotkey conflicts with Spotlight")

    async def test_long_embed_text_is_cut_for_the_snippet(self):
        db = await asyncpg.connect(database_url())
        await db.execute("update source_records set embed_text = repeat('x', 1000) where object_id = $1", SLACK)
        await db.close()
        [hit] = await retrieval.search("icon cache release", k=1, sources=["slack"])
        self.assertEqual(hit["snippet"], "x" * 300)

    async def test_the_query_gets_the_instruction_prefix_and_a_wrong_dimension_is_a_tool_error(self):
        await retrieval.search(QUESTION, k=1)
        self.assertEqual(self.stub.requests, [[
            "Instruct: Given a question about a software project, retrieve relevant passages that answer it\n"
            "Query: " + QUESTION]])
        self.assertEqual(self.stub.auth, ["Bearer test-key"])
        self.stub.dimension = 1023
        async with Client(server.mcp) as client:
            result = await client.call_tool("search", {"question": QUESTION})
        self.assertTrue(result.is_error)
        self.assertIn("returned 1023 dimensions", result.content[0].text)

    async def test_hand_written_questions_find_their_photon_records(self):
        """Check the seed/embed/search wiring with exact distances, not ANN recall or model quality.

        The sparse hash stub makes HNSW misses sensitive to index construction. Use an exact scan here
        while preserving the same questions and top-three requirements. HNSW has its own controlled test;
        retrieval quality and approximate recall on real embeddings belong in the benchmark.
        """
        with contextlib.redirect_stdout(io.StringIO()):
            await seed_photon.main()
        questions = {
            "Why can't I use dots in branch names?": "C08PHOTONDV/1789392360.000100",
            "Caps Lock turns on when I use it as a Hyper key": "10087",  # PHO-87
            "Why do launched programs stay hidden behind other windows?": "C08PHOTONBG/1789971000.000100",
            "Running a development build quits the release app": "10105",  # PHO-105, Photon-Dev
            "Pressing the arrow keys in clipboard history doesn't move between items": "10036",  # PHO-36
        }
        with search_settings(enable_indexscan="off"):
            for question, object_id in questions.items():
                with self.subTest(question):
                    hits = await retrieval.search(question, k=3)
                    self.assertIn(object_id, [h["object_id"] for h in hits])

    async def test_without_an_api_key_no_authorization_header_is_sent(self):
        key = os.environ.pop("EMBEDDING_API_KEY")
        try:
            [hit] = await retrieval.search(QUESTION, k=1)
        finally:
            os.environ["EMBEDDING_API_KEY"] = key
        self.assertEqual(hit["object_id"], "10004")
        self.assertEqual(self.stub.auth, [None])

    async def test_stdio_server_lists_and_calls_its_tools(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "ember_mcp"], cwd=HERE, env={
            "DATABASE_URL": database_url(), "EMBEDDING_URL": self.stub.url, "EMBEDDING_API_KEY": "test-key"})
        async with Client(params) as client:
            tools = await client.list_tools()
            self.assertEqual(sorted(t.name for t in tools.tools), ["get_record", "search"])
            result = await client.call_tool("search", {"question": QUESTION, "k": 1})
        self.assertFalse(result.is_error)
        [hit] = result.structured_content["result"]
        self.assertEqual((hit["source"], hit["object_id"]), ("jira", "10004"))

    async def test_sessions_are_read_only(self):
        conn = await retrieval.connect()
        try:
            with self.assertRaises(asyncpg.ReadOnlySQLTransactionError):
                await conn.execute("delete from source_records")
        finally:
            await conn.close()

    async def test_missing_database_url_is_a_tool_error(self):
        url = os.environ.pop("DATABASE_URL")
        try:
            async with Client(server.mcp) as client:
                result = await client.call_tool("search", {"question": "hotkey"})
        finally:
            os.environ["DATABASE_URL"] = url
        self.assertTrue(result.is_error)
        self.assertIn("DATABASE_URL is not set", result.content[0].text)


def prototype_schema() -> str:
    """The graph.* DDL from the research prototype (ADR 0011), read without importing it."""
    for node in ast.parse(PROTOTYPE.read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "SCHEMA":
            return ast.literal_eval(node.value)
    raise AssertionError("no SCHEMA in the prototype")


REPO_ROW = ("github", "repository", "acme/photon", "r1", 1, {"payload": {"full_name": "acme/photon"}})
COMMENT_ROW = ("jira", "comment", "10004/comment/1", "c1", 23, {"payload": {
    "comment": {"id": "1", "body": "Spotlight owns cmd-space, pick option-space"},
    "issue": {"id": "10004", "key": "PHO-4", "self": ISSUE_SELF}}})
COMMENT = "10004/comment/1"
# Each record's own node as extract writes it in its episode: (source, kind, object_id, label, key)
OWN = [("jira", "issue", "10004", "WorkItem", "PHO-4"), ("jira", "comment", COMMENT, "Message", "1"),
       ("github", "commit", COMMIT, "Change", COMMIT), ("github", "pull_request", PULL, "Change", PULL),
       ("github", "repository", "acme/photon", "Container", "acme/photon"),
       ("slack", "message", SLACK, "Message", SLACK.replace("/", ":"))]
PERSON, PLACEHOLDER = ("jira", "Person", "jira:u1"), ("jira", "WorkItem", "PHO-5")
# (type, src, dst) as (source, label, key). CONTAINS is a hub edge, a Person has no record, a placeholder is skipped.
EDGES = [("REFERENCES", ("jira", "Message", "1"), ("jira", "WorkItem", "PHO-4")),
         ("AUTHORED", PERSON, ("jira", "WorkItem", "PHO-4")),
         ("RELATES_TO", ("jira", "WorkItem", "PHO-4"), PLACEHOLDER),
         ("CONTAINS", ("github", "Container", "acme/photon"), ("github", "Change", COMMIT))]
GRAPH_NODES = [(s, label, key) for s, _k, _o, label, key in OWN] + [PERSON, PLACEHOLDER]


async def add_episodes() -> None:
    """The comment and repository rows (never embedded), and an episode per record whose own node is in OWN."""
    db = await asyncpg.connect(database_url())
    await db.executemany(
        "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
        " values ($1, 'acme', $2, $3, $4, $5, $6)",
        [(source, kind, object_id, version, datetime(2026, 9, day, tzinfo=timezone.utc), json.dumps(record))
         for source, kind, object_id, version, day, record in (REPO_ROW, COMMENT_ROW)])
    for source, kind, object_id, label, key in OWN:
        attrs = {"nodes": [{"label": label, "key": key, "props": {"kind": kind, "external_id": key}},
                           {"label": "Person", "key": "jira:u1", "props": {"name": "Ana"}}], "edges": []}
        await db.execute(
            """insert into episodes (key, tenant, source, kind, occurred_at, text, attrs, source_record_id, extractor_version)
               select 'ep:' || id, tenant, source, kind, occurred_at, '', $4, id, 1 from source_records
               where (source, kind, object_id) = ($1, $2, $3) order by occurred_at desc limit 1""",
            source, kind, object_id, json.dumps(attrs))
    await db.close()


class GraphBackendTest(Database):
    """The seed-and-expand backends on ROWS plus the comment and repository rows, GRAPH_NODES and EDGES."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await add_episodes()
        db = await asyncpg.connect(database_url())
        await db.execute(prototype_schema())
        await db.executemany(
            "insert into graph.nodes (uuid, tenant, source, label, key, placeholder) values (gen_random_uuid(), 'acme', $1, $2, $3, $4)",
            [(*n, n == PLACEHOLDER) for n in GRAPH_NODES])
        await db.executemany(
            """insert into graph.edges (uuid, tenant, source, type, src, dst, valid_at)
               select gen_random_uuid(), 'acme', s.source, $1, s.id, d.id, now() from graph.nodes s, graph.nodes d
               where (s.source, s.label, s.key) = ($2, $3, $4) and (d.source, d.label, d.key) = ($5, $6, $7)""",
            [(t, *a, *b) for t, a, b in EDGES])
        await db.close()
        self.addAsyncCleanup(self.drop_graph)

    async def drop_graph(self):
        db = await asyncpg.connect(database_url())
        await db.execute("drop schema if exists graph cascade")
        await db.close()

    async def check_expansion(self, search):
        """The jira comment comes back through PHO-4, scored below it; the hub, Person and placeholder never do."""
        hits = await search(QUESTION, 50, None, None)
        seeds = await retrieval.search(QUESTION, k=50)
        issue = next(h for h in hits if h["object_id"] == "10004")
        comment = next(h for h in hits if h["object_id"] == COMMENT)
        self.assertEqual(comment["via"], {"seed_source": "jira", "seed_object_id": "10004", "edge": "REFERENCES"})
        self.assertAlmostEqual(comment["score"], graph.GRAPH_WEIGHT * issue["score"])
        self.assertEqual(comment["url"], "https://photon.atlassian.net/browse/PHO-4")
        self.assertEqual([h for h in hits if "via" not in h], seeds)
        self.assertNotIn("acme/photon", [h["object_id"] for h in hits])
        self.assertEqual([h["object_id"] for h in await search(QUESTION, 1, None, None)], ["10004"])
        self.assertNotIn(COMMENT, [h["object_id"] for h in await search(QUESTION, 50, ["github"], None)])
        self.assertEqual(await search(QUESTION, 50, None, "other"), [])

    async def test_pggraph_expands_one_hop_over_the_graph_tables(self):
        await self.check_expansion(graph.pggraph_search)

    async def test_pggraph_without_its_tables_is_a_tool_error(self):
        await self.drop_graph()
        with patch.dict(os.environ, RETRIEVAL_BACKEND="pggraph"):
            async with Client(server.mcp) as client:
                result = await client.call_tool("search", {"question": QUESTION})
        self.assertTrue(result.is_error)
        self.assertIn("graph.nodes and graph.edges tables", result.content[0].text)

    async def test_neo4j_maps_seeds_to_the_loaders_node_keys(self):
        """CI has no Neo4j, so the driver call is stubbed: it gets each seed's (tenant, source, label, key) and
        answers what NEO4J_NEIGHBOURS would for this graph."""
        calls = []

        async def stub(_conn, nodes):
            calls.append(nodes)
            if ("acme", "jira", "WorkItem", "PHO-4") not in nodes:
                return []
            pho4 = nodes.index(("acme", "jira", "WorkItem", "PHO-4"))
            return [(pho4, "REFERENCES", ("acme", "jira", "Message", "1")), (pho4, "AUTHORED", ("acme", *PERSON))]

        with patch.object(graph, "neo4j_neighbours", stub):
            await self.check_expansion(graph.neo4j_search)
        self.assertCountEqual(calls[0], [("acme", source, label, key) for source, kind, object_id, label, key in OWN
                                         if object_id in ("10004", PULL, SLACK, COMMIT)])

    @unittest.skipUnless(os.environ.get("NEO4J_TEST_URI"), "NEO4J_TEST_URI is not set")
    async def test_neo4j_expands_one_hop_in_a_real_neo4j(self):
        import neo4j

        auth = ("neo4j", os.environ["NEO4J_PASSWORD"])
        with neo4j.GraphDatabase.driver(os.environ["NEO4J_TEST_URI"], auth=auth) as driver:
            driver.execute_query("MATCH (n) WHERE n.group_id STARTS WITH 'acme_' DETACH DELETE n")
            for source, label, key in GRAPH_NODES:  # as load writes them: group_id, source, external_id
                driver.execute_query(f"CREATE (:{label} {{group_id: $g, source: $s, external_id: $k, placeholder: $p}})",
                                     g=f"acme_{source}", s=source, k=key, p=(source, label, key) == PLACEHOLDER)
            for t, (s1, l1, k1), (s2, l2, k2) in EDGES:
                driver.execute_query(f"MATCH (a:{l1} {{group_id: $g1, external_id: $k1}}), (b:{l2} {{group_id: $g2, external_id: $k2}}) "
                                     f"CREATE (a)-[:{t}]->(b)", g1=f"acme_{s1}", k1=k1, g2=f"acme_{s2}", k2=k2)
            try:
                with patch.dict(os.environ, NEO4J_URI=os.environ["NEO4J_TEST_URI"]):
                    await self.check_expansion(graph.neo4j_search)
            finally:
                driver.execute_query("MATCH (n) WHERE n.group_id STARTS WITH 'acme_' DETACH DELETE n")

    async def test_neo4j_without_config_and_an_unknown_backend_are_tool_errors(self):
        for env, message in (({"RETRIEVAL_BACKEND": "neo4j", "NEO4J_URI": ""}, "needs NEO4J_URI and NEO4J_PASSWORD"),
                             ({"RETRIEVAL_BACKEND": "neo4j", "NEO4J_URI": "bolt://127.0.0.1:1", "NEO4J_PASSWORD": "x"},
                              "Can't read Ember's Neo4j"),
                             ({"RETRIEVAL_BACKEND": "elastic"}, "is not one of pgvector, neo4j, pggraph")):
            with self.subTest(env), patch.dict(os.environ, env):
                async with Client(server.mcp) as client:
                    result = await client.call_tool("search", {"question": QUESTION})
                self.assertTrue(result.is_error)
                self.assertIn(message, result.content[0].text)

    async def test_graph_hits_keep_the_hit_shape_through_the_tool(self):
        with patch.dict(os.environ, RETRIEVAL_BACKEND="pggraph"):
            async with Client(server.mcp) as client:
                result = await client.call_tool("search", {"question": QUESTION, "k": 2})
        self.assertFalse(result.is_error)
        issue, comment = result.structured_content["result"]
        self.assertEqual(set(comment) - {"via"}, set(issue))
        self.assertEqual(comment["via"]["seed_object_id"], "10004")


if __name__ == "__main__":
    unittest.main()
