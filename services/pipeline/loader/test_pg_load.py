"""The extract and load workers against a real Postgres (EMBER-85). Skipped unless PGTEST_URL is set.

Needs dbmate and a Postgres server with pgvector it may create and drop a database on (CI's
"Pipeline loader against Postgres" step), from services/pipeline:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python -m unittest loader.test_pg_load -v

It builds the repo migrations in database ember_test_pg_load and loads into a MemoryWriter. With
NEO4J_TEST_URI (and NEO4J_PASSWORD) set it also loads into that Neo4j, under the throwaway tenant
it-pg-load, which it resets.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from loader.memory_writer import MemoryWriter
from loader.workers import ExtractWorker, LoadWorker, serve

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_pg_load"
PGTEST_URL = os.environ.get("PGTEST_URL")
NEO4J_URI = os.environ.get("NEO4J_TEST_URI")
TENANT = "it-pg-load"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def database_url(name: str = DB) -> str:
    parts = urlsplit(PGTEST_URL)
    return urlunsplit(parts._replace(path="/" + name))


def photon_rows() -> list[tuple]:
    """apps/mcp/seed_photon.py's rows: the shapes the enrich workers store, from the photon fixtures."""
    spec = importlib.util.spec_from_file_location("seed_photon", REPO / "apps" / "mcp" / "seed_photon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.rows()


def message(ts: str, text: str, channel: str = "C1") -> dict:
    return {"type": "message", "channel": channel, "ts": ts, "user": "U1", "text": text}


class SlowWriter(MemoryWriter):
    """Signals when a write starts, then takes a while, so a stop can land mid-job."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = seconds
        self.started = threading.Event()

    def write_nodes(self, nodes):
        self.started.set()
        time.sleep(self.seconds)
        return super().write_nodes(nodes)


class DownWriter(MemoryWriter):
    """Neo4j unreachable until `down` is cleared."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def write_nodes(self, nodes):
        if self.down:
            from neo4j.exceptions import ServiceUnavailable

            raise ServiceUnavailable("neo4j is down (test)")
        return super().write_nodes(nodes)


@unittest.skipUnless(PGTEST_URL, "PGTEST_URL not set")
class PgLoadTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        async def recreate():
            conn = await asyncpg.connect(PGTEST_URL)
            try:
                await conn.execute(f"drop database if exists {DB}")
                await conn.execute(f"create database {DB}")
            finally:
                await conn.close()

        asyncio.run(recreate())
        result = subprocess.run([DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS),
                                 "--no-dump-schema", "migrate"], capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"dbmate failed:\n{result.stdout}{result.stderr}")

    @classmethod
    def tearDownClass(cls):
        async def drop():
            conn = await asyncpg.connect(PGTEST_URL)
            try:
                await conn.execute(f"drop database if exists {DB} with (force)")
            finally:
                await conn.close()

        asyncio.run(drop())

    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate source_records, episodes restart identity cascade")

    async def asyncTearDown(self):
        await self.db.close()

    async def insert(self, source, kind, object_id, version, occurred_at, record, tenant=TENANT) -> int:
        return await self.db.fetchval(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, $2, $3, $4, $5, $6, $7::jsonb) returning id",
            source, tenant, kind, object_id, version, occurred_at, json.dumps(record))

    async def seed_photon(self) -> list[tuple]:
        rows = photon_rows()
        await self.db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, $2, $3, $4, $5, $6, $7::jsonb)",
            [(source, TENANT, kind, object_id, version, at, json.dumps({"payload": payload}))
             for source, kind, object_id, version, at, payload in rows])
        return rows

    async def extract_all(self, **kwargs) -> ExtractWorker:
        worker = ExtractWorker(database_url(), **kwargs)
        await worker.drain()
        return worker

    async def load_all(self, writer, **kwargs) -> LoadWorker:
        worker = LoadWorker(database_url(), writer, **kwargs)
        await worker.drain()
        return worker

    async def unloaded(self) -> int:
        return await self.db.fetchval("select count(*) from episodes where loaded_at is null")

    def slack_message(self, writer, key="C1:1789353000.000100") -> dict:
        from loader.records import node_uuid

        return writer.nodes[("Message", node_uuid(f"{TENANT}_slack", "Message", key))]

    # --- the photon data, end to end ---

    async def test_photon_jira_and_slack_load_into_their_own_subgraphs_once(self):
        rows = await self.seed_photon()
        issues = sum(1 for r in rows if r[:2] == ("jira", "issue"))
        comments = sum(1 for r in rows if r[:2] == ("jira", "comment"))
        messages = len({r[2] for r in rows if r[0] == "slack"})
        self.assertTrue(issues and comments and messages)

        extract = await self.extract_all()
        self.assertEqual(extract.counts["extracted"], len(rows), dict(extract.counts))
        self.assertEqual(await self.db.fetchval("select count(*) from episodes"), len(rows))
        self.assertEqual(await self.unloaded(), len(rows))

        writer = MemoryWriter()
        load = await self.load_all(writer, batch=100)  # several batches per subgraph
        self.assertEqual(load.counts["loaded"], len(rows))
        self.assertEqual(await self.unloaded(), 0)

        jira, slack = f"{TENANT}_jira", f"{TENANT}_slack"
        facts = writer.read_facts([jira])
        self.assertEqual(facts.node_counts["WorkItem"], issues)
        self.assertEqual(facts.node_counts["Message"], comments)
        self.assertEqual(facts.relationships[("Message", "REFERENCES", "WorkItem")], comments)
        self.assertEqual(facts.missing_required, {}, "a placeholder was never filled in")
        facts = writer.read_facts([slack])
        self.assertEqual(facts.node_counts["Message"], messages)
        self.assertEqual(facts.missing_required, {})
        self.assertFalse(any(p.get("placeholder") for p in writer.nodes.values()))
        self.assertEqual({p["group_id"] for p in writer.nodes.values()}, {jira, slack})

        # A rerun of both stages finds nothing to do and changes nothing.
        before = copy.deepcopy((writer.nodes, writer.edges))
        stamps = await self.db.fetch("select id, loaded_at from episodes order by id")
        extract = await self.extract_all()
        load = await self.load_all(writer)
        self.assertEqual(extract.counts["extracted"], 0)
        self.assertEqual(load.counts["loaded"], 0)
        self.assertEqual((writer.nodes, writer.edges), before)
        self.assertEqual(await self.db.fetch("select id, loaded_at from episodes order by id"), stamps)

        if NEO4J_URI:
            await self.check_neo4j_matches(writer)

    async def check_neo4j_matches(self, memory: MemoryWriter) -> None:
        from loader.neo4j_writer import Neo4jWriter
        from ontology import tenant_subgraphs

        neo4j = Neo4jWriter(NEO4J_URI, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
        try:
            neo4j.reset(tenant_subgraphs(TENANT))
            await self.db.execute("update episodes set loaded_at = null")
            await self.load_all(neo4j)
            await self.db.execute("update episodes set loaded_at = null")
            await self.load_all(neo4j)  # a replay adds nothing
            for group in tenant_subgraphs(TENANT):
                got, want = neo4j.read_facts([group]), memory.read_facts([group])
                self.assertEqual(got.node_counts, want.node_counts, group)
                self.assertEqual(got.relationships, want.relationships, group)
                self.assertEqual(got.missing_required, {}, group)
        finally:
            neo4j.reset(tenant_subgraphs(TENANT))
            neo4j.close()

    # --- ordering ---

    async def test_out_of_order_arrival_applies_in_source_order(self):
        # The edit arrives (and gets the lower id) before the original.
        await self.insert("slack", "message", "C1/1789353000.000100", "1789353300.0",
                          T0 + timedelta(minutes=5), {"payload": message("1789353000.000100", "edited")})
        await self.insert("slack", "message", "C1/1789353000.000100", "1789353000.000100",
                          T0, {"payload": message("1789353000.000100", "original")})
        await self.extract_all()
        order = await self.db.fetch("select text from episodes order by occurred_at, id")
        self.assertIn("original", order[0]["text"])
        writer = MemoryWriter()
        await self.load_all(writer)
        self.assertEqual(self.slack_message(writer)["embed_text"], "edited")

    async def test_a_late_older_edit_does_not_regress_the_graph(self):
        await self.insert("slack", "message", "C1/1789353000.000100", "1789353300.0",
                          T0 + timedelta(minutes=5), {"payload": message("1789353000.000100", "newest")})
        writer = MemoryWriter()
        await self.extract_all()
        await self.load_all(writer)
        await self.insert("slack", "message", "C1/1789353000.000100", "1789353100.0",
                          T0 + timedelta(minutes=1), {"payload": message("1789353000.000100", "older edit")})
        await self.extract_all()
        load = await self.load_all(writer)
        self.assertEqual(load.counts["loaded"], 1)
        self.assertEqual(self.slack_message(writer)["embed_text"], "newest")
        if NEO4J_URI:
            await self.check_neo4j_matches(writer)
            from loader.neo4j_writer import Neo4jWriter

            neo4j = Neo4jWriter(NEO4J_URI, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
            try:
                await self.db.execute("update episodes set loaded_at = null")
                await self.load_all(neo4j)
                # the older edit loaded after the newer one, in a separate batch
                await self.db.execute("update episodes set loaded_at = null where text like '%older edit%'")
                await self.load_all(neo4j)
                [found] = neo4j._run("MATCH (m:Message {group_id: $g, external_id: 'C1:1789353000.000100'}) "
                                     "RETURN m.embed_text AS text", g=f"{TENANT}_slack")
                self.assertEqual(found["text"], "newest")
            finally:
                from ontology import tenant_subgraphs

                neo4j.reset(tenant_subgraphs(TENANT))
                neo4j.close()

    async def test_a_slack_delete_clears_the_message_and_a_late_edit_does_not_restore_it(self):
        ts = "1789353000.000100"
        await self.insert("slack", "message", f"C1/{ts}", ts, T0, {"payload": message(ts, "hello")})
        writer = MemoryWriter()
        await self.extract_all()
        await self.load_all(writer)
        self.slack_message(writer).update(embedding=[0.1, 0.2], embedding_text_hash="h")  # as the embed step leaves it

        await self.insert("slack", "message", f"C1/{ts}", "deleted:1789354000.0", T0 + timedelta(hours=1),
                          {"payload": None, "message": None, "deleted": True})
        extract = await self.extract_all()
        self.assertEqual(extract.counts["rejected"], 0)
        await self.load_all(writer)
        await self.insert("slack", "message", f"C1/{ts}", "1789353500.0", T0 + timedelta(minutes=8),
                          {"payload": message(ts, "late edit")})
        await self.extract_all()
        await self.load_all(writer)
        self.assertEqual(await self.unloaded(), 0)
        stored = self.slack_message(writer)
        self.assertIs(stored["deleted"], True)
        self.assertEqual(set(stored), {"group_id", "uuid", "source", "external_id", "kind", "deleted", "occurred_at"},
                         "deleted content survived in the graph")

        if NEO4J_URI:
            from loader.neo4j_writer import Neo4jWriter
            from ontology import tenant_subgraphs

            neo4j = Neo4jWriter(NEO4J_URI, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
            group = f"{TENANT}_slack"
            try:
                neo4j.reset(tenant_subgraphs(TENANT))
                # in source order: the message (then embedded), the delete, then the late, older edit
                for text in ("hello", "deleted", "late edit"):
                    await self.db.execute("update episodes set loaded_at = null where text like $1", f"%{text}%")
                    await self.load_all(neo4j)
                    if text == "hello":
                        neo4j._run("MATCH (m:Message {group_id: $g}) SET m.embedding = [0.1, 0.2], "
                                   "m.embedding_text_hash = 'h'", g=group)
                [found] = neo4j._run(f"MATCH (m:Message {{group_id: $g, external_id: 'C1:{ts}'}}) "
                                     "RETURN properties(m) AS p", g=group)
                self.assertEqual(set(found["p"]), {"group_id", "uuid", "source", "external_id", "kind", "deleted",
                                                   "occurred_at"})
            finally:
                neo4j.reset(tenant_subgraphs(TENANT))
                neo4j.close()

    async def test_an_older_delete_arriving_after_a_newer_edit_leaves_the_message_live(self):
        ts = "1789353000.000100"
        writers = [MemoryWriter()]
        if NEO4J_URI:
            from loader.neo4j_writer import Neo4jWriter
            from ontology import tenant_subgraphs

            neo4j = Neo4jWriter(NEO4J_URI, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
            neo4j.reset(tenant_subgraphs(TENANT))
            writers.append(neo4j)
        try:
            steps = [("1789353300.0", T0 + timedelta(minutes=5), {"payload": message(ts, "edit at 5")}),
                     ("deleted:1789353180.0", T0 + timedelta(minutes=3), {"payload": None, "deleted": True}),
                     ("1789353420.0", T0 + timedelta(minutes=7), {"payload": message(ts, "edit at 7")})]
            for index, (version, at, record) in enumerate(steps):
                await self.insert("slack", "message", f"C1/{ts}", version, at, record)
                await self.extract_all()
                for writer in writers:
                    await self.db.execute("update episodes set loaded_at = null")
                    await self.load_all(writer)
                for writer in writers:
                    props = self.message_props(writer, ts)
                    self.assertNotIn("deleted", props, f"step {index}: {type(writer).__name__}")
                    self.assertEqual(props["embed_text"], "edit at 7" if index == 2 else "edit at 5")
        finally:
            if NEO4J_URI:
                neo4j.reset(tenant_subgraphs(TENANT))
                neo4j.close()

    def message_props(self, writer, ts: str) -> dict:
        if isinstance(writer, MemoryWriter):
            return self.slack_message(writer, f"C1:{ts}")
        [found] = writer._run("MATCH (m:Message {group_id: $g, external_id: $e}) RETURN properties(m) AS p",
                              g=f"{TENANT}_slack", e=f"C1:{ts}")
        return found["p"]

    async def test_a_re_extract_during_a_load_is_not_marked_loaded(self):
        await self.insert("slack", "message", "C1/1789353000.000100", "1",
                          T0, {"payload": message("1789353000.000100", "v1")})
        await self.extract_all()
        loop, db = asyncio.get_running_loop(), self.db

        class ReExtractMidWrite(MemoryWriter):
            """Extract upserts a newer extractor version between load's read and its stamp."""
            done = False

            def write_nodes(self, nodes):
                if not self.done:
                    self.done = True
                    asyncio.run_coroutine_threadsafe(db.execute(
                        "update episodes set extractor_version = extractor_version + 1, loaded_at = null,"
                        " text = 'v2', updated_at = clock_timestamp()"), loop).result(10)
                return super().write_nodes(nodes)

        worker = LoadWorker(database_url(), ReExtractMidWrite())
        try:
            self.assertTrue(await worker.load_subgraph(TENANT, "slack"))  # one pass
            self.assertEqual(worker.counts["loaded"], 0)
            self.assertEqual(await self.unloaded(), 1, "the re-extracted episode was marked loaded")
            self.assertTrue(await worker.load_subgraph(TENANT, "slack"))  # the next pass writes the new version
            self.assertEqual(worker.counts["loaded"], 1)
            self.assertEqual(await self.unloaded(), 0)
        finally:
            await worker.engine.dispose()

    # --- worker behavior ---

    async def test_both_workers_catch_up_at_start_and_wake_on_notify_within_a_second(self):
        before = await self.insert("slack", "message", "C1/1789353000.000100", "1",
                                   T0, {"payload": message("1789353000.000100", "while stopped")})
        writer = MemoryWriter()
        extract = ExtractWorker(database_url(), poll_seconds=60)  # only NOTIFY can explain a fast pickup
        load = LoadWorker(database_url(), writer, poll_seconds=60)
        tasks = [asyncio.create_task(extract.run()), asyncio.create_task(load.run())]
        try:
            await self.until("select loaded_at is not null from episodes where source_record_id = $1", before)
            await asyncio.wait_for(asyncio.gather(extract.listening.wait(), load.listening.wait()), 5)
            started = time.monotonic()
            after = await self.insert("slack", "message", "C1/1789353001.000100", "1",
                                      T0, {"payload": message("1789353001.000100", "while running")})
            await self.until("select true from episodes where source_record_id = $1", after)
            extracted = time.monotonic()
            await self.until("select loaded_at is not null from episodes where source_record_id = $1", after)
            self.assertLess(extracted - started, 1.0, "extract did not wake on NOTIFY source_records")
            self.assertLess(time.monotonic() - extracted, 1.0, "load did not wake on NOTIFY episodes")
        finally:
            extract.stop()
            load.stop()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)

    async def until(self, query: str, *args, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not await self.db.fetchval(query, *args):
            self.assertLess(time.monotonic(), deadline, f"timed out: {query}")
            await asyncio.sleep(0.01)

    @unittest.skipIf(sys.platform == "win32", "SIGTERM is POSIX")
    async def test_sigterm_mid_job_finishes_the_job_in_flight(self):
        await self.insert("slack", "message", "C1/1789353000.000100", "1",
                          T0, {"payload": message("1789353000.000100", "in flight")})
        await self.extract_all()
        writer = SlowWriter(seconds=1.0)
        worker = LoadWorker(database_url(), writer, poll_seconds=60)
        task = asyncio.create_task(serve(worker, once=False))
        await asyncio.wait_for(asyncio.to_thread(writer.started.wait), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 10)
        self.assertTrue(worker.stopping.is_set())
        self.assertEqual(await self.unloaded(), 0, "the job in flight was not finished and stamped")
        self.assertEqual(self.slack_message(writer)["embed_text"], "in flight")

    async def test_a_writer_failure_leaves_episodes_unloaded_until_it_recovers(self):
        await self.insert("slack", "message", "C1/1789353000.000100", "1",
                          T0, {"payload": message("1789353000.000100", "waits for neo4j")})
        await self.extract_all()
        writer = DownWriter()
        load = await self.load_all(writer)
        self.assertEqual(load.counts["transient_failures"], 1)
        self.assertEqual(await self.unloaded(), 1)
        self.assertEqual(writer.nodes, {})

        writer.down = False
        load = await self.load_all(writer)  # a new process, as after a restart or the backoff
        self.assertEqual(load.counts["loaded"], 1)
        self.assertEqual(await self.unloaded(), 0)

    async def test_bad_and_unsupported_records_are_counted_not_retried(self):
        await self.insert("slack", "message", "C1/1", "1", T0, {"payload": None})  # nothing to map
        await self.insert("github", "issue", "o/r#1", "1", T0, {"payload": {}})  # no extractor yet
        await self.insert("slack", "message", "C1/1789353000.000100", "1",
                          T0, {"payload": message("1789353000.000100", "fine")})
        worker = await self.extract_all()
        self.assertEqual(worker.counts["rejected"], 1)
        self.assertEqual(worker.counts["extracted"], 1)
        self.assertEqual(worker.counts["not_extracted:github:issue"], 1)
        self.assertEqual(await self.db.fetchval("select count(*) from episodes"), 1)

    async def test_a_new_extractor_version_re_extracts_and_reloads(self):
        from loader import extract as extract_module
        from loader import workers

        await self.insert("slack", "message", "C1/1789353000.000100", "1",
                          T0, {"payload": message("1789353000.000100", "v1")})
        await self.extract_all()
        await self.load_all(MemoryWriter())
        original = extract_module.EXTRACTOR_VERSION
        workers.EXTRACTOR_VERSION = original + 1
        try:
            worker = await self.extract_all()
        finally:
            workers.EXTRACTOR_VERSION = original
        self.assertEqual(worker.counts["extracted"], 1)
        row = await self.db.fetchrow("select extractor_version, loaded_at from episodes")
        self.assertEqual(row["extractor_version"], original + 1)
        self.assertIsNone(row["loaded_at"])
        self.assertEqual(await self.db.fetchval("select count(*) from episodes"), 1)


if __name__ == "__main__":
    unittest.main()
