"""The embed worker against a real Postgres with pgvector (EMBER-77). The embedding service is a
stub HTTP server, never the Spark.

Needs psql, dbmate, the packages in requirements.txt, and a pgvector Postgres server it may create
and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/embed/test_embed.py

It builds the repo migrations in database ember_test_embed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import embed  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_embed"


def database_url() -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def stub_vector(text: str, dimension: int = 1024) -> list[float]:
    """Hashed bag of words, so texts sharing words are near each other by cosine."""
    vector = [0.0] * dimension
    for word in text.lower().split():
        vector[zlib.crc32(word.encode()) % dimension] += 1.0
    return vector


def pg_vector(vector: list[float]) -> str:
    return "[" + ",".join(map(str, vector)) + "]"


class StubEmbeddings:
    """POST /v1/embeddings. `statuses` are answered, in order, before any success. A text containing
    "poison" gets a 1023-long vector, which vector(1024) rejects."""

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.statuses: list[int] = []
        self.requests: list[list[str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body["input"])
                assert self.path == "/v1/embeddings" and self.headers["Authorization"] == "Bearer test-key"
                if stub.statuses:
                    self.send_response(stub.statuses.pop(0))
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                data = [{"index": i, "embedding": stub_vector(text, stub.dimension - ("poison" in text))}
                        for i, text in enumerate(body["input"])]
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

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def adf(*paragraphs: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]} for text in paragraphs]}


# (source, kind, record, expected embed_text)
RECORDS = [
    ("github", "issue",
     {"payload": {"action": "opened", "issue": {"number": 42, "title": "Login redirect drops query", "body": "Steps: log in"}}},
     "Login redirect drops query\n\nSteps: log in"),
    ("github", "pull_request",
     {"payload": {"pull_request": {"title": "Keep the query string", "body": "Fixes #42"}},
      "fetched": {"files": [{"filename": "app/login.py"}, {"filename": "app/urls.py"}]}},
     "Keep the query string\n\nFixes #42\n\napp/login.py\napp/urls.py"),
    ("github", "issue_comment",
     {"payload": {"issue": {"title": "Login redirect drops query"}, "comment": {"body": "Reproduced on staging"}}},
     "Login redirect drops query\n\nReproduced on staging"),
    ("github", "commit",
     {"payload": {"id": "a" * 40, "message": "Fix login redirect", "added": ["app/new.py"], "modified": ["app/login.py"], "removed": []}},
     "Fix login redirect\n\napp/new.py\napp/login.py"),
    ("jira", "issue",
     {"payload": {"webhookEvent": "jira:issue_created", "issue": {"key": "EMBER-77", "fields": {
         "summary": "pgvector embeddings", "description": adf("Embed every row.", "Store embed_text.")}}}},
     "EMBER-77: pgvector embeddings\n\nEmbed every row.\nStore embed_text."),
    ("jira", "comment",
     {"payload": {"issue": {"key": "EMBER-77", "fields": {"summary": "pgvector embeddings"}},
                  "comment": {"body": adf("Use HNSW.")}}},
     "EMBER-77: pgvector embeddings\n\nUse HNSW."),
    ("gitlab", "merge_request",
     {"payload": {"object_kind": "merge_request", "object_attributes": {"title": "Add intake", "description": "One listener"}}},
     "Add intake\n\nOne listener"),
    ("gitlab", "note",
     {"payload": {"object_kind": "note", "object_attributes": {"note": "Looks good"}, "merge_request": {"title": "Add intake"}}},
     "Add intake\n\nLooks good"),
    ("gitlab", "commit",
     {"payload": {"id": "b" * 40, "message": "Add intake\n", "added": [], "modified": ["intake.py"], "removed": ["old.py"]}},
     "Add intake\n\nintake.py\nold.py"),
    ("slack", "message",
     {"payload": {"type": "event_callback", "event": {"type": "message", "text": "Shared intake is type plus body."}}},
     "Shared intake is type plus body."),
    ("teams", "message", {"payload": {"id": "1", "body": {"content": "hi"}}},
     '{"payload":{"body":{"content":"hi"},"id":"1"}}'),
]


class EmbedTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
            "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
        run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")

    @classmethod
    def tearDownClass(cls):
        run("psql", "-X", "-q", "-d", os.environ["PGTEST_URL"], "-c", f"drop database if exists {DB}")

    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate source_records restart identity cascade")
        self.stub = StubEmbeddings()

    async def asyncTearDown(self):
        await self.db.close()
        await asyncio.to_thread(self.stub.close)

    def worker(self, **kwargs) -> embed.Worker:
        client = embed.EmbeddingClient(self.stub.url, "test-key", base_delay=0.01)
        return embed.Worker(database_url(), client, **kwargs)

    async def insert(self, source: str, kind: str, record: dict) -> int:
        return await self.db.fetchval(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, 'acme', $2, gen_random_uuid()::text, 'v1', now(), $3::jsonb) returning id",
            source, kind, json.dumps(record))

    async def embedded(self, row_id: int):
        return await self.db.fetchrow(
            "select embed_text, vector_dims(embedding) as dims, embedded_at from source_records where id = $1", row_id)

    async def drain(self, worker: embed.Worker) -> None:
        try:
            await worker._drain()
        finally:
            await worker.engine.dispose()

    async def test_each_source_gets_readable_text_and_a_vector_once(self):
        ids = [await self.insert(source, kind, record) for source, kind, record, _ in RECORDS]
        await self.drain(self.worker(batch=4))

        first = {}
        for row_id, (source, kind, _record, expected) in zip(ids, RECORDS):
            row = await self.embedded(row_id)
            self.assertEqual(row["embed_text"], expected, f"{source} {kind}")
            self.assertEqual(row["dims"], 1024)
            first[row_id] = row["embedded_at"]
        self.assertEqual(len(self.stub.requests), 3, "one request per batch of 4")

        await self.drain(self.worker())
        self.assertEqual(len(self.stub.requests), 3, "a second run embedded something again")
        for row_id in ids:
            self.assertEqual((await self.embedded(row_id))["embedded_at"], first[row_id])

    def test_long_records_are_cut(self):
        text = embed.embed_text("slack", "message", {"payload": {"text": "x" * 20000}})
        self.assertEqual(len(text), embed.MAX_CHARS)

    async def test_catches_up_at_start_and_wakes_on_insert(self):
        before = await self.insert("slack", "message", {"payload": {"text": "inserted while stopped"}})
        worker = self.worker(poll_seconds=60)  # so only the NOTIFY can explain a fast pickup
        task = asyncio.create_task(worker.run())
        try:
            await self.until(lambda: self.embedded(before), timeout=5)
            started = time.monotonic()
            after = await self.insert("slack", "message", {"payload": {"text": "inserted while running"}})
            await self.until(lambda: self.embedded(after), timeout=5)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            worker.stop()
            await asyncio.wait_for(task, 10)

    async def until(self, fetch, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while (await fetch())["embedded_at"] is None:
            self.assertLess(time.monotonic(), deadline, "row was not embedded in time")
            await asyncio.sleep(0.02)

    async def test_retries_throttling_and_server_errors_and_skips_a_bad_row(self):
        good = await self.insert("slack", "message", {"payload": {"text": "fine"}})
        bad = await self.insert("slack", "message", {"payload": {"text": "poison"}})
        also_good = await self.insert("slack", "message", {"payload": {"text": "also fine"}})
        self.stub.statuses = [429, 503]
        worker = self.worker()
        await self.drain(worker)

        self.assertEqual(len(self.stub.requests), 3, "429, 503, then success")
        self.assertEqual((await self.embedded(good))["embed_text"], "fine")
        self.assertEqual((await self.embedded(also_good))["embed_text"], "also fine")
        self.assertIsNone((await self.embedded(bad))["embedded_at"])
        self.assertEqual(worker.failed, {bad})

    async def test_refuses_to_start_on_a_wrong_dimension(self):
        self.stub.dimension = 768
        row = await self.insert("slack", "message", {"payload": {"text": "never embedded"}})
        with self.assertRaises(embed.IdentityMismatch):
            await self.worker().run()
        self.assertEqual(self.stub.requests, [["ember embedding identity check"]])
        self.assertIsNone((await self.embedded(row))["embedded_at"])

    async def test_cosine_nearest_neighbours_use_the_hnsw_index(self):
        ids = {text: await self.insert("slack", "message", {"payload": {"text": text}}) for text in (
            "login redirect drops the query string", "postgres backup schedule", "slack bot token rotation",
            "neo4j memory settings", "grafana dashboard for intake")}
        await self.drain(self.worker())

        query = """select id from source_records where embedding is not null
                   order by embedding <=> $1::text::vector limit 3"""
        probe = pg_vector(stub_vector("why does login redirect drop the query"))
        async with self.db.transaction():
            await self.db.execute("set local enable_seqscan = off")
            plan = "\n".join(row[0] for row in await self.db.fetch("explain " + query, probe))
            self.assertIn("source_records_embedding", plan)
            nearest = [row["id"] for row in await self.db.fetch(query, probe)]
        self.assertEqual(nearest[0], ids["login redirect drops the query string"])
        self.assertEqual(len(nearest), 3)


if __name__ == "__main__":
    unittest.main()
