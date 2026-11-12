"""ember_mcp against a real Postgres with pgvector and a stub embedding server (EMBER-79, EMBER-78, EMBER-166).

Needs psql, dbmate, the packages in requirements.txt, services/embed/requirements.txt (the photon
seed embeds through the embed worker) and services/pipeline/requirements.txt (the graph tests load
through extract and load with GRAPH_STORE=postgres's writer), and a Postgres server with the vector
extension it may create and drop a database on, and create a login role on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 apps/mcp/test_mcp.py

It builds the repo migrations in database ember_test_mcp, the web app's Better Auth tables, and login role
ember_mcp_test in ember_graph_reader, which the server connects as.
"""

from __future__ import annotations

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
sys.path.insert(0, str(REPO / "services" / "pipeline"))

import seed_photon  # noqa: E402
from ember_mcp import retrieval, server  # noqa: E402
from loader.pg_writer import PgWriter  # noqa: E402
from loader.workers import ExtractWorker, LoadWorker  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_mcp"
ISSUE_SELF = "https://photon.atlassian.net/rest/api/2/issue/10004"
READER, READER_PASSWORD = "ember_mcp_test", "ember-mcp-test"
ADA, BO = "ada@acme.test", "bo@globex.test"

# The web app's Better Auth tables (schema web), the columns ember_user_tenants reads, as Better Auth names them.
# Ada is in organizations acme and photon and has a personal workspace; Bo is in globex only.
WEB = """
create schema web;
create table web."user" (id text primary key, name text not null, email text not null unique);
create table web.organization (id text primary key, name text not null, slug text not null unique,
                               tenant text not null unique, personal boolean);
create table web.member (id text primary key, "organizationId" text not null references web.organization,
                         "userId" text not null references web."user", role text not null);
insert into web."user" values ('u-ada', 'Ada', 'Ada@acme.test'), ('u-bo', 'Bo', 'bo@globex.test');
insert into web.organization values ('o-acme', 'Acme', 'acme', 'acme', false), ('o-photon', 'Photon', 'photon', 'photon', false),
  ('o-ada', 'Ada', 'ada-personal', 'ada-personal', true), ('o-globex', 'Globex', 'globex', 'globex', false);
insert into web.member values ('m1', 'o-acme', 'u-ada', 'owner'), ('m2', 'o-photon', 'u-ada', 'member'),
  ('m3', 'o-ada', 'u-ada', 'owner'), ('m4', 'o-globex', 'u-bo', 'owner');
"""

SITE = "https://acme.atlassian.net"


def database_url(user: str | None = None, password: str | None = None) -> str:
    """The test database, as PGTEST_URL's user (the tables' owner) or as user."""
    parts = urlsplit(os.environ["PGTEST_URL"])
    if user:
        parts = parts._replace(netloc=f"{user}:{password}@{parts.hostname}:{parts.port}")
    return urlunsplit(parts._replace(path="/" + DB))


def jira(key: str, summary: str, assignee: str, status: str) -> dict:
    """A jira:issue_updated record as enrich:jira stores it, on site acme or globex."""
    site = SITE if key.startswith("ACME") else "https://globex.atlassian.net"
    number = key.rpartition("-")[2]
    return {"payload": {"webhookEvent": "jira:issue_updated", "issue": {
        "id": f"20{number}", "key": key, "self": f"{site}/rest/api/3/issue/20{number}",
        "fields": {"summary": summary, "project": {"key": key.rpartition("-")[0]}, "status": {"name": status},
                   "assignee": {"accountId": assignee, "displayName": assignee.title()}}}}}


def slack(ts: str, text: str) -> dict:
    return {"payload": {"type": "message", "channel": "C1", "ts": ts, "user": "U1", "text": text, "team": "TACME"}}


# (tenant, source, kind, object_id, version, day, record) loaded into the graph: ACME-1 moves from Dana to
# Eli, a Slack thread names it, Globex has its own ticket, and Ada's personal workspace has an issue.
GRAPH_ROWS = [
    ("acme", "jira", "issue", "201", "v1", 1, jira("ACME-1", "Rotate the login signing key", "dana", "To Do")),
    ("acme", "jira", "issue", "201", "v2", 3, jira("ACME-1", "Rotate the login signing key", "eli", "In Progress")),
    ("acme", "slack", "message", "C1/1790000000.000100", "1790000000.000100", 2,
     slack("1790000000.000100", "Eli takes ACME-1 since Dana is out, the signing key rotation can't wait")),
    ("globex", "jira", "issue", "202", "v1", 1, jira("GLX-2", "Rotate the login signing key at Globex", "fay", "To Do")),
    ("ada-personal", "github", "issue", "ada/notes#1", "v1", 1, {"payload": {"action": "opened", "repository": {"full_name": "ada/notes"},
     "issue": {"number": 1, "title": "Signing key notes", "body": "What I learned rotating keys", "state": "open",
               "user": {"login": "ada"}, "html_url": "https://github.com/ada/notes/issues/1",
               "created_at": "2026-10-01T12:00:00Z", "updated_at": "2026-10-01T12:00:00Z"}}}),
]


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


class McpTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
            "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
        run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")
        run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", database_url(), "-c", WEB, "-c",
            f"""do $$ begin
                  if not exists (select from pg_roles where rolname = '{READER}') then
                    create role {READER} login password '{READER_PASSWORD}' in role ember_graph_reader;
                  end if;
                end $$""")
        cls.stub = StubEmbeddings()
        os.environ.update(DATABASE_URL=database_url(READER, READER_PASSWORD), EMBER_USER_EMAIL=ADA,
                          EMBEDDING_URL=cls.stub.url, EMBEDDING_API_KEY="test-key")

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
        await db.execute("truncate source_records, episodes, graph.graphs restart identity cascade")
        await db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record, embed_text, embedding)"
            " values ($1, 'acme', $2, $3, $4, $5, $6, $7, $8::text::vector)",
            [(source, kind, object_id, version, datetime(2026, 9, day, tzinfo=timezone.utc), json.dumps(record),
              text, None if text is None else "[" + ",".join(map(str, stub_vector(text))) + "]")
             for source, kind, object_id, version, day, record, text in ROWS])
        await db.close()

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
        with search_settings(enable_seqscan="off", **{"ember.tenants": "acme"}):
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

        record = await retrieval.get_record("jira", "10004", raw=True)
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
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, DATABASE_URL=database_url()):
            await seed_photon.main()  # writes, so as the owner
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

    async def test_seeded_jira_issues_carry_their_changelog_histories(self):
        """The photon changelog pages ride on each issue as issue.changelog.histories, the expand=changelog shape,
        and the Slack users and channels are stored too."""
        with patch.dict(os.environ, DATABASE_URL=database_url()):
            await seed_photon.main(embed_rows=False)
        record = await retrieval.get_record("jira", "10015", raw=True)  # PHO-15, read back as Ada through the MCP
        issue = record["record"]["payload"]["issue"]
        moves = [(item["fromString"], item["toString"]) for entry in issue["changelog"]["histories"]
                 for item in entry["items"] if item["field"] == "status"]
        self.assertEqual((issue["key"], moves), ("PHO-15", [("To Do", "In Progress"), ("In Progress", "Done")]))
        # Slack users and channels, as enrich:slack stores users.info and conversations.info: the object itself
        user = (await retrieval.get_record("slack", "U08DREYES01", raw=True))["record"]
        channel = (await retrieval.get_record("slack", "C08PHOTONBG", raw=True))["record"]
        self.assertEqual((user["real_name"], channel["name"]), ("Daniel Reyes", "photon-bugs"))

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
            "DATABASE_URL": database_url(READER, READER_PASSWORD), "EMBER_USER_EMAIL": ADA,
            "EMBEDDING_URL": self.stub.url, "EMBEDDING_API_KEY": "test-key"})
        async with Client(params) as client:
            tools = await client.list_tools()
            self.assertEqual(sorted(t.name for t in tools.tools), ["get_record", "history", "search", "whoami"])
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

    # --- the graph (EMBER-166) ---

    async def load_graph(self) -> None:
        """GRAPH_ROWS through extract and load with the Postgres graph writer, as the pipeline runs them."""
        db = await asyncpg.connect(database_url())
        await db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($2, $1, $3, $4, $5, $6, $7)",
            [(*row[:5], datetime(2026, 10, row[5], tzinfo=timezone.utc), json.dumps(row[6])) for row in GRAPH_ROWS])
        await db.close()
        await ExtractWorker(database_url()).drain()
        await LoadWorker(database_url(), PgWriter()).drain()

    @contextlib.contextmanager
    def as_user(self, email: str):
        with patch.dict(os.environ, EMBER_USER_EMAIL=email):
            yield

    async def test_tenants_are_every_organization_and_the_personal_workspace(self):
        await self.load_graph()
        ada = await retrieval.whoami()
        self.assertEqual(ada["tenants"], ["acme", "ada-personal", "photon"])
        self.assertEqual({(g["tenant"], g["source"], g["scope"]) for g in ada["graphs"]},
                         {("acme", "jira", "acme.atlassian.net/ACME"), ("acme", "slack", "TACME"),
                          ("acme", "github", "acme/photon"), ("acme", "jira", "photon.atlassian.net/PHO"),  # ROWS
                          ("ada-personal", "github", "ada/notes")})
        with self.as_user(BO):
            bo = await retrieval.whoami()
        self.assertEqual((bo["tenants"], [g["scope"] for g in bo["graphs"]]), (["globex"], ["globex.atlassian.net/GLX"]))

    async def test_a_reader_sees_only_its_tenants_graphs_records_and_facts(self):
        await self.load_graph()
        hits = await retrieval.search("Rotate the login signing key", k=50)
        self.assertEqual({h["object_id"] for h in hits if h["source"] in ("jira", "github")} & {"202", "ada/notes#1", "201"},
                         {"201", "ada/notes#1"})
        self.assertIsNone(await retrieval.get_record("jira", "202"))
        self.assertIsNone(await retrieval.history("jira", "202"))
        with self.as_user(BO):
            self.assertIsNone(await retrieval.get_record("jira", "201"))
            self.assertEqual([h["object_id"] for h in await retrieval.search("Rotate the login signing key")], ["202"])
            async with retrieval.reader() as scope:
                seen = await scope.conn.fetchval(
                    """select array_agg(distinct tenant order by tenant) from (
                         select tenant from graph.nodes union all select tenant from graph.edges
                         union all select tenant from source_records union all select tenant from episodes) t""")
        self.assertEqual(seen, ["globex"])

    async def test_search_fuses_vector_and_graph_hits_and_adds_current_facts(self):
        await self.load_graph()
        # Embedded but never loaded, so only vector search finds it. The graph's records have no embedding.
        db = await asyncpg.connect(database_url())
        await db.execute(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record, embed_text, embedding)"
            " values ('slack', 'acme', 'message', 'C9/1', '1', now(), '{}', $1, $2::text::vector)",
            "who owns the signing key rotation now", "[" + ",".join(map(str, stub_vector("who owns the signing key rotation now"))) + "]")
        await db.close()
        hits = {h["object_id"]: h for h in await retrieval.search("Who owns the signing key rotation now?", k=10)}
        self.assertIn("C9/1", hits)
        self.assertIn("201", hits)
        facts = [f["fact"] for f in hits["201"]["related"]]
        self.assertIn("Eli is assigned to Rotate the login signing key", facts)
        self.assertNotIn("Dana is assigned to Rotate the login signing key", facts)
        self.assertLessEqual(len(facts), 5)

        with patch.dict(os.environ, EMBEDDING_URL=""):  # BM25 and graph traversal only
            hits = await retrieval.search("signing key rotation", k=10, graphs=["acme.atlassian.net/ACME"])
        self.assertEqual([h["object_id"] for h in hits], ["201"])
        self.assertEqual(self.stub.requests[-1:], [["Instruct: " + retrieval.TASK + "\nQuery: Who owns the signing key rotation now?"]])

    async def test_get_record_returns_the_node_its_current_facts_and_graph(self):
        await self.load_graph()
        record = await retrieval.get_record("jira", "201")
        self.assertEqual(record["version"], "v2")
        self.assertEqual(record["node"], {"label": "WorkItem", "key": "ACME-1", "name": "Rotate the login signing key",
                                          "summary": ""})
        self.assertEqual(record["graph"], {"name": "acme.atlassian.net/ACME", "scope": "acme.atlassian.net/ACME", "source": "jira"})
        facts = {f["fact"] for f in record["facts"]}
        self.assertIn("Eli is assigned to Rotate the login signing key", facts)
        self.assertIn("Rotate the login signing key has status In Progress", facts)
        self.assertNotIn("Dana is assigned to Rotate the login signing key", facts)
        # The Slack message's link edge into the Jira graph
        self.assertTrue(any(f["type"] == "REFERENCES" and f["fact"].endswith("references Rotate the login signing key")
                            for f in record["facts"]), facts)

    async def test_an_exact_key_comes_first_then_what_references_it(self):
        await self.load_graph()
        hits = await retrieval.search("ACME-1", k=5)
        self.assertEqual([(h["source"], h["object_id"], h["score"]) for h in hits[:2]],
                         [("jira", "201", 1.0), ("slack", "C1/1790000000.000100", 0.5)])
        [hit] = await retrieval.search("what is ada/notes#1 about?", k=1)
        self.assertEqual((hit["source"], hit["object_id"], hit["score"]), ("github", "ada/notes#1", 1.0))

    async def test_get_record_takes_a_jira_key_and_is_compact_unless_raw(self):
        await self.load_graph()
        record = await retrieval.get_record("jira", "ACME-1")
        self.assertEqual({k: record[k] for k in ("object_id", "key", "status", "assignee", "url")},
                         {"object_id": "201", "key": "ACME-1", "status": "In Progress", "assignee": "Eli",
                          "url": "https://acme.atlassian.net/browse/ACME-1"})
        self.assertNotIn("record", record)
        self.assertEqual(len(await retrieval.history("jira", "ACME-1")), len(await retrieval.history("jira", "201")))
        raw = await retrieval.get_record("jira", "ACME-1", raw=True)
        self.assertEqual(raw["record"]["payload"]["issue"]["fields"]["assignee"]["accountId"], "eli")

    async def test_history_returns_ended_facts_with_the_episodes_that_set_and_ended_them(self):
        await self.load_graph()
        history = {f["fact"]: f for f in await retrieval.history("jira", "201")}
        dana, eli = history["Dana is assigned to Rotate the login signing key"], history["Eli is assigned to Rotate the login signing key"]
        self.assertEqual((dana["valid_at"], dana["invalid_at"]), ("2026-10-01T00:00:00+00:00", "2026-10-03T00:00:00+00:00"))
        self.assertEqual((dana["set_by"]["version"], dana["ended_by"]["version"]), ("v1", "v2"))
        self.assertEqual((dana["set_by"]["source"], dana["set_by"]["object_id"]), ("jira", "201"))
        self.assertEqual((eli["invalid_at"], eli["set_by"]["version"], eli["ended_by"]), (None, "v2", None))
        self.assertIsNotNone(history["Rotate the login signing key has status To Do"]["invalid_at"])

    async def test_an_unknown_email_or_an_owner_connection_gets_no_access(self):
        await self.load_graph()
        with self.as_user("nobody@acme.test"):
            self.assertEqual((await retrieval.whoami())["tenants"], [])
            async with Client(server.mcp) as client:
                for tool, args in (("search", {"question": "signing key"}), ("get_record", {"source": "jira", "object_id": "201"}),
                                   ("history", {"source": "jira", "object_id": "201"})):
                    result = await client.call_tool(tool, args)
                    self.assertTrue(result.is_error, tool)
                    self.assertIn("belongs to no Ember organization", result.content[0].text)
            async with retrieval.reader(need_tenants=False) as scope:  # row-level security with no tenants
                self.assertEqual(await scope.conn.fetchval(
                    "select (select count(*) from graph.nodes) + (select count(*) from source_records)"), 0)
        with patch.dict(os.environ, DATABASE_URL=database_url()):
            with self.assertRaisesRegex(retrieval.AccessError, "not the tables' owner"):
                await retrieval.whoami()


if __name__ == "__main__":
    unittest.main()
