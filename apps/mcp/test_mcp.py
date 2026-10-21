"""ember_mcp against a real Postgres with pgvector and a stub embedding server (EMBER-79, EMBER-78).

Needs psql, dbmate, the packages in requirements.txt and services/embed/requirements.txt (the photon
seed embeds through the embed worker), and a Postgres server with the vector extension
it may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 apps/mcp/test_mcp.py

It builds the repo migrations in database ember_test_mcp.
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

import asyncpg
from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import seed_photon  # noqa: E402
from ember_mcp import retrieval, server  # noqa: E402

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


class StubEmbeddings:
    """POST /v1/embeddings with stub_vector. Records each request's input; `dimension` sets the vector length."""

    def __init__(self) -> None:
        self.dimension = 1024
        self.requests: list[list[str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == "/v1/embeddings" and self.headers["Authorization"] == "Bearer test-key"
                stub.requests.append(body["input"])
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
        db = await asyncpg.connect(database_url())
        await db.execute("truncate source_records, episodes restart identity")
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
        self.stub.dimension = 1023
        async with Client(server.mcp) as client:
            result = await client.call_tool("search", {"question": QUESTION})
        self.assertTrue(result.is_error)
        self.assertIn("returned 1023 dimensions", result.content[0].text)

    async def test_hand_written_questions_find_their_photon_records(self):
        """EMBER-78's acceptance questions over the photon synthetic data, embedded by the seed's path.
        Each expected record is in the top 3. The third and fourth share few words with their records."""
        with contextlib.redirect_stdout(io.StringIO()):
            await seed_photon.main()
        questions = {
            "Why can't I use dots in branch names?": "C08PHOTONDV/1789392360.000100",
            "Caps Lock turns on when I use it as a Hyper key": "10087",  # PHO-87
            "Why do launched programs stay hidden behind other windows?": "C08PHOTONBG/1789971000.000100",
            "Running a development build quits the release app": "10105",  # PHO-105, Photon-Dev
            "Pressing the arrow keys in clipboard history doesn't move between items": "10036",  # PHO-36
        }
        for question, object_id in questions.items():
            with self.subTest(question):
                hits = await retrieval.search(question, k=3)
                self.assertIn(object_id, [h["object_id"] for h in hits])

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


if __name__ == "__main__":
    unittest.main()
