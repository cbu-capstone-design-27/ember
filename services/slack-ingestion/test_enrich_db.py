"""enrich:slack against a real Postgres: EMBER-67's acceptance criteria, one test each where possible.

Needs dbmate, the libraries in requirements.txt, and a Postgres server it may create and drop a
database on, e.g. the compose one or CI's:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/slack-ingestion/test_enrich_db.py

dbmate builds database ember_test_enrich from infra/k8s/apps/postgres/migrations, as the cluster's
migrate Job does. The migrations include the trigger that sends NOTIFY raw_events on insert
(EMBER-75). Slack is FakeSlackApi from test_enrich.py.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
sys.path.insert(0, str(HERE))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import enrich  # noqa: E402
from enrich import Worker, run_async  # noqa: E402
from models import Base, RateLimit, RawEvent, SourceRecord  # noqa: E402
from pg_gates import PgRateGate  # noqa: E402
from test_enrich import CHANNEL, CHANNELS, PARENT, TOKEN, USERS, FakeSlackApi, callback, message  # noqa: E402

PGTEST_URL = os.environ.get("PGTEST_URL", "")
DBMATE = os.environ.get("DBMATE", "dbmate")
TEST_DB = "ember_test_enrich"
TENANT = "acme"


def database_url(name: str) -> str:
    return urlunsplit(urlsplit(PGTEST_URL)._replace(path="/" + name))


def dbmate(*args: str) -> None:
    command = [DBMATE, "--url", database_url(TEST_DB), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", *args]
    subprocess.run(command, check=True, capture_output=True)


def setUpModule():
    if not PGTEST_URL:
        raise unittest.SkipTest("set PGTEST_URL to a Postgres server to run these tests")
    dbmate("drop")
    dbmate("up")


def tearDownModule():
    if PGTEST_URL:
        dbmate("drop")


class EnrichDatabaseTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.url = database_url(TEST_DB)
        cls.engine = create_engine(enrich.database_urls(cls.url)[0])

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(text("truncate raw_events, source_records, rate_limits restart identity cascade"))

    # ---- helpers ----

    def worker(self, fake: FakeSlackApi, **overrides) -> Worker:
        options = dict(token=TOKEN, exchange=fake, rates={}, concurrency=1, poll_seconds=30.0, max_attempts=3)
        options.update(overrides)
        return Worker(self.url, **options)

    def insert(self, body: dict, *, tenant: str | None = TENANT, event_id: str | None = None) -> int:
        if event_id:
            body = dict(body, event_id=event_id)
        row = enrich.raw_event_row({"type": "slack", "body": body}, tenant)
        with self.engine.begin() as conn:
            return conn.execute(pg_insert(RawEvent).values(**row).returning(RawEvent.id)).scalar_one()

    def raw(self, row_id: int) -> RawEvent:
        with Session(self.engine) as session:
            return session.get(RawEvent, row_id)

    def records(self, kind: str | None = None) -> list[SourceRecord]:
        stmt = select(SourceRecord).order_by(SourceRecord.id)
        if kind:
            stmt = stmt.where(SourceRecord.kind == kind)
        with Session(self.engine) as session:
            return list(session.scalars(stmt))

    def pending(self) -> int:
        with self.engine.begin() as conn:
            return conn.execute(select(func.count()).select_from(RawEvent).where(RawEvent.enriched_at.is_(None))).scalar_one()

    async def until(self, check, timeout: float = 10.0, every: float = 0.02):
        """Wait until check() (run in a thread) is truthy; returns its value."""
        deadline = time.monotonic() + timeout
        while True:
            value = await asyncio.to_thread(check)
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"timed out after {timeout}s waiting for {check}")
            await asyncio.sleep(every)

    async def started(self, worker: Worker) -> asyncio.Task:
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(worker.listening.wait(), 10)
        return task

    async def stopped(self, worker: Worker, task: asyncio.Task) -> None:
        worker.stop()
        await asyncio.wait_for(task, 20)

    # ---- the models ----

    def test_models_match_the_schema_the_migrations_build(self):
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                rows = conn.execute(
                    text("select column_name, is_nullable from information_schema.columns "
                         "where table_schema = 'public' and table_name = :t order by ordinal_position"),
                    {"t": table.name},
                ).all()
                with self.subTest(table=table.name):
                    self.assertEqual([(c.name, c.nullable) for c in table.columns], [(name, nullable == "YES") for name, nullable in rows])
            indexes = conn.execute(text("select indexname from pg_indexes where tablename = 'raw_events'")).scalars().all()
            self.assertIn("raw_events_unenriched", indexes)

    # ---- acceptance criteria ----

    def test_message_reply_edit_and_delete_become_records_with_their_context(self):
        fake = FakeSlackApi()
        new = self.insert(callback(message("1700000200.000100"), event_id="Ev1"))
        reply = self.insert(callback(message("1700000300.000100", user="U0BEN", thread_ts=PARENT["ts"]), event_id="Ev2"))
        edit = self.insert(callback({"type": "message", "subtype": "message_changed", "channel": CHANNEL, "ts": "1700000400.000300",
                                     "message": message("1700000200.000100", text="edited", edited={"ts": "1700000400.000000"})}, event_id="Ev3"))
        delete = self.insert(callback({"type": "message", "subtype": "message_deleted", "channel": CHANNEL, "ts": "1700000500.000400",
                                       "deleted_ts": "1700000300.000100", "previous_message": message("1700000300.000100", user="U0BEN")}, event_id="Ev4"))

        async def scenario():
            worker = self.worker(fake)
            task = await self.started(worker)
            await self.until(lambda: self.pending() == 0)
            await self.stopped(worker, task)

        run_async(scenario())
        for row_id in (new, reply, edit, delete):
            row = self.raw(row_id)
            self.assertIsNotNone(row.enriched_at)

        messages = {(r.object_id, r.version): r for r in self.records("message")}
        first = messages[(f"{CHANNEL}/1700000200.000100", "1700000200.000100")]
        self.assertEqual((first.record["user"]["id"], first.record["channel"]["name"]), ("U0ADA", "decisions"))
        self.assertEqual(first.raw_event_id, new)
        replied = messages[(f"{CHANNEL}/1700000300.000100", "1700000300.000100")]
        self.assertEqual(replied.record["thread_parent"]["ts"], PARENT["ts"])
        self.assertEqual(replied.record["user"]["id"], "U0BEN")
        self.assertIn((f"{CHANNEL}/{PARENT['ts']}", PARENT["ts"]), messages)  # the fetched parent is stored too
        edited = messages[(f"{CHANNEL}/1700000200.000100", "1700000400.000000")]
        self.assertEqual(edited.record["message"]["text"], "edited")
        tombstone = messages[(f"{CHANNEL}/1700000300.000100", "deleted:1700000500.000400")]
        self.assertTrue(tombstone.record["deleted"])

        # Users and the channel are fetched once and then read from their records
        self.assertEqual(sorted(r.object_id for r in self.records("user")), ["U0ADA", "U0BEN"])
        self.assertEqual([r.object_id for r in self.records("channel")], [CHANNEL])

    def test_user_channel_and_membership_events_become_records(self):
        fake = FakeSlackApi()
        self.insert(callback(message("1700000200.000100"), event_id="Ev1"))  # stores U0ADA and the channel
        changed = {**USERS["U0ADA"], "real_name": "Ada L.", "updated": 1700000300}
        self.insert(callback({"type": "user_change", "user": changed, "event_ts": "1700000300.000100"}, event_id="Ev2"))
        self.insert(callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "adr", "created": 1690000000}},
                             event_id="Ev3", event_time=1700000400))
        self.insert(callback({"type": "member_joined_channel", "user": "U0BEN", "channel": CHANNEL, "channel_type": "C"},
                             event_id="Ev4", event_time=1700000500))

        async def enrich_all():
            worker = self.worker(fake)
            task = await self.started(worker)
            await self.until(lambda: self.pending() == 0)
            await self.stopped(worker, task)

        run_async(enrich_all())
        users = {(r.object_id, r.version): r.record for r in self.records("user")}
        self.assertEqual(users[("U0ADA", "1700000300")]["real_name"], "Ada L.")
        channels = [r.record for r in self.records("channel")]
        self.assertEqual(channels[-1], {**CHANNELS[CHANNEL], "name": "adr"})  # merged onto the stored channel
        [membership] = self.records("membership")
        self.assertEqual((membership.object_id, membership.version), (f"{CHANNEL}/U0BEN", "1700000500"))
        self.assertEqual((membership.record["member"], membership.record["user"]["id"]), (True, "U0BEN"))
        self.assertEqual(fake.count("conversations.info"), 1)  # only for the first message

        # Reprocessing every row adds nothing
        before = len(self.records())
        with self.engine.begin() as conn:
            conn.execute(text("update raw_events set enriched_at = null"))
        run_async(enrich_all())
        self.assertEqual(len(self.records()), before)
        # U0ADA for the message and U0BEN for the join, the channel once, and no thread to fetch
        self.assertEqual((fake.count("users.info"), fake.count("conversations.info"), fake.count("conversations.replies")), (2, 1, 0))

    def test_rows_are_enriched_in_slacks_order_not_arrival_order(self):
        # A retry or a late delivery arrives after newer events; ties on Slack's time break on id
        late = self.insert(callback(message("1700000100.000100")), event_id="Ev-late")
        newest = self.insert(callback(message("1700000300.000100")), event_id="Ev-newest")
        oldest = self.insert(callback(message("1700000050.000100")), event_id="Ev-oldest")
        # Two channel events in the same whole second (event_time): a tie, broken on id
        tie_a = self.insert(callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "adr"}},
                                     event_time=1700000100), event_id="Ev-tie-a")
        tie_b = self.insert(callback({"type": "channel_archive", "channel": CHANNEL}, event_time=1700000100),
                            event_id="Ev-tie-b")
        when = {i: self.raw(i).occurred_at for i in (late, newest, oldest)}
        self.assertEqual(when[oldest], datetime(2023, 11, 14, 22, 14, 10, 100, tzinfo=timezone.utc))  # event_ts, exactly

        async def scenario():
            worker = self.worker(FakeSlackApi(), concurrency=1)  # one runner, so claim order is enrich order
            task = asyncio.create_task(worker.run())
            await self.until(lambda: self.pending() == 0)
            await self.stopped(worker, task)

        run_async(scenario())
        with self.engine.begin() as conn:
            order = conn.execute(text("select id from raw_events order by enriched_at")).scalars().all()
        # 1700000050.0001, then the tie at 1700000100 by id, then 1700000100.0001, then 1700000300.0001
        self.assertEqual(order, [oldest, tie_a, tie_b, late, newest])

    def test_a_notify_wakes_the_idle_worker_within_a_second(self):
        fake = FakeSlackApi()

        async def scenario():
            worker = self.worker(fake, poll_seconds=60.0)  # so only the NOTIFY can wake it
            task = await self.started(worker)
            await asyncio.sleep(0.3)  # idle: the start-up claim found nothing
            begin = time.monotonic()
            row_id = await asyncio.to_thread(self.insert, callback(message("1700000200.000100")))
            await self.until(lambda: self.raw(row_id).enriched_at is not None, timeout=5.0, every=0.01)
            took = time.monotonic() - begin
            await self.stopped(worker, task)
            return took

        took = run_async(scenario())
        self.assertLess(took, 1.0, f"took {took:.3f}s from insert to enriched")

    def test_rows_inserted_while_it_was_stopped_are_picked_up_at_start(self):
        ids = [self.insert(callback(message(f"17000002{i:02d}.000100")), event_id=f"Ev{i}") for i in range(3)]

        async def scenario():
            worker = self.worker(FakeSlackApi(), poll_seconds=60.0)
            task = asyncio.create_task(worker.run())
            await self.until(lambda: self.pending() == 0, timeout=5.0)
            await self.stopped(worker, task)

        run_async(scenario())
        self.assertTrue(all(self.raw(i).enriched_at is not None for i in ids))

    def test_reprocessing_the_same_delivery_adds_no_records(self):
        row_id = self.insert(callback(message("1700000200.000100", thread_ts=PARENT["ts"])))

        async def scenario():
            worker = self.worker(FakeSlackApi())
            task = await self.started(worker)
            await self.until(lambda: self.raw(row_id).enriched_at is not None)
            before = len(self.records())
            # Run the same row again, as a replay would
            await asyncio.to_thread(self._reset_and_notify, row_id)
            await self.until(lambda: self.raw(row_id).enriched_at is not None)
            await self.stopped(worker, task)
            return before

        before = run_async(scenario())
        self.assertEqual(len(self.records()), before)
        self.assertEqual(before, 4)  # channel, user, the fetched parent, the reply
        # The same delivery_id is stored once, however often it is sent
        added, duplicate, _ignored = self._seed_twice()
        self.assertEqual((added, duplicate), (1, 1))

    def _reset_and_notify(self, row_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("update raw_events set enriched_at = null where id = :id"), {"id": row_id})
            conn.execute(text("select pg_notify('raw_events', 'slack')"))

    def _seed_twice(self):
        fixture = str(REPO / "packages" / "ingestion-envelope" / "fixtures" / "slack.json")
        first = enrich.seed(self.url, fixture, TENANT)
        second = enrich.seed(self.url, fixture, TENANT)
        return first[0], second[1], second[2]

    def test_several_rows_run_at_once(self):
        users = {f"U{i}": {"id": f"U{i}", "updated": 1700000000 + i} for i in range(8)}
        fake = FakeSlackApi(users=users, delay={"users.info": 0.4})
        for i in range(8):
            self.insert(callback(message(f"17000002{i:02d}.000100", user=f"U{i}")), event_id=f"Ev{i}")

        async def scenario():
            worker = self.worker(fake, concurrency=4)
            begin = time.monotonic()
            task = asyncio.create_task(worker.run())
            await self.until(lambda: self.pending() == 0, timeout=10.0)
            took = time.monotonic() - begin
            await self.stopped(worker, task)
            return took

        took = run_async(scenario())
        self.assertGreaterEqual(fake.max_in_flight, 2)
        self.assertLess(took, 8 * 0.4, f"8 rows took {took:.2f}s; they did not overlap")

    def test_throttles_and_pacing_are_kept_in_rate_limits(self):
        fake = FakeSlackApi(throttle={"users.info": 1})  # one 429 with Retry-After: 1
        row_id = self.insert(callback(message("1700000200.000100")))

        async def scenario():
            worker = self.worker(fake)
            task = await self.started(worker)
            await self.until(lambda: self.raw(row_id).enriched_at is not None)
            await self.stopped(worker, task)

        run_async(scenario())
        with Session(self.engine) as session:
            buckets = {r.bucket: r for r in session.scalars(select(RateLimit).where(RateLimit.tenant == TENANT))}
        self.assertIn("users.info", buckets)
        self.assertIn("conversations.info", buckets)
        self.assertEqual(fake.count("users.info"), 2)  # the throttled call, then the retry

        # Pacing: a second caller waits one interval after the first, across gates sharing a row
        slept: list[float] = []
        first = PgRateGate(self.engine, TENANT, "conversations.history", 0.5, sleep=slept.append)
        second = PgRateGate(self.engine, TENANT, "conversations.history", 0.5, sleep=slept.append)
        first.wait()
        second.wait()
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 0.5, delta=0.1)
        # extend() holds every caller of the bucket
        slept.clear()
        first.extend(2.0)
        PgRateGate(self.engine, TENANT, "conversations.history", 0.0, sleep=slept.append).wait()
        self.assertAlmostEqual(slept[0], 2.0, delta=0.2)

    def test_ctrl_c_finishes_the_job_in_flight(self):
        fake = FakeSlackApi(delay={"users.info": 1.5})
        row_id = self.insert(callback(message("1700000200.000100")))

        async def scenario():
            worker = self.worker(fake)
            task = await self.started(worker)
            await self.until(lambda: "users.info" in fake.started)  # the job is mid-call
            worker.stop()  # what Ctrl+C does (enrich.serve)
            await asyncio.wait_for(task, 20)

        run_async(scenario())
        row = self.raw(row_id)
        self.assertIsNotNone(row.enriched_at)
        self.assertEqual(len(self.records("message")), 1)

    def test_a_real_sigint_through_serve_finishes_the_job_in_flight(self):
        fake = FakeSlackApi(delay={"users.info": 1.5})
        row_id = self.insert(callback(message("1700000200.000100")))

        async def scenario():
            worker = self.worker(fake)
            served = asyncio.create_task(enrich.serve(worker))  # installs the Ctrl+C handling main() uses
            await self.until(lambda: "users.info" in fake.started)
            signal.raise_signal(signal.SIGINT)  # what pressing Ctrl+C delivers
            await asyncio.wait_for(served, 20)

        run_async(scenario())
        self.assertIsNotNone(self.raw(row_id).enriched_at)
        self.assertEqual(len(self.records("message")), 1)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)  # serve put it back

    def test_an_error_no_retry_fixes_is_not_retried(self):
        fake = FakeSlackApi(errors={"users.info": "invalid_auth"})
        row_id = self.insert(callback(message("1700000200.000100")))

        async def scenario():
            worker = self.worker(fake, poll_seconds=0.2)
            task = await self.started(worker)
            await self.until(lambda: row_id in worker._given_up)
            await asyncio.sleep(0.8)  # several polls
            await self.stopped(worker, task)

        with self.assertLogs("slack-enrich", "ERROR") as logs:
            run_async(scenario())
        self.assertTrue(any("invalid_auth" in line for line in logs.output), logs.output)
        self.assertIsNone(self.raw(row_id).enriched_at)  # still pending, so a restart tries it again
        self.assertEqual(fake.count("users.info"), 1)
        self.assertEqual(self.records(), [])

    def test_a_failing_row_is_retried_until_the_maximum(self):
        fake = FakeSlackApi(garbage={"users.info"})  # answers 200 with a body that is not JSON
        row_id = self.insert(callback(message("1700000200.000100")))

        async def scenario():
            worker = self.worker(fake, poll_seconds=0.2, max_attempts=3)
            task = await self.started(worker)
            await self.until(lambda: row_id in worker._given_up)
            await asyncio.sleep(0.6)
            await self.stopped(worker, task)

        run_async(scenario())
        self.assertIsNone(self.raw(row_id).enriched_at)
        self.assertEqual(fake.count("users.info"), 3)
        self.assertEqual(self.records(), [])  # each failed try rolled back what it wrote

    def test_failures_are_kept_in_memory_so_a_restart_tries_again(self):
        fake = FakeSlackApi(errors={"users.info": "invalid_auth"})
        row_id = self.insert(callback(message("1700000200.000100")))

        async def run_until_given_up():
            worker = self.worker(fake, poll_seconds=0.2)
            task = await self.started(worker)
            await self.until(lambda: row_id in worker._given_up)
            await self.stopped(worker, task)

        with self.assertLogs("slack-enrich", "ERROR"):
            run_async(run_until_given_up())
        fake.errors.clear()  # the token is fixed, then the worker restarts

        async def restarted():
            worker = self.worker(fake, poll_seconds=0.2)
            task = await self.started(worker)
            await self.until(lambda: self.raw(row_id).enriched_at is not None)
            await self.stopped(worker, task)

        run_async(restarted())
        self.assertEqual([r.kind for r in self.records("message")], ["message"])

    def test_rows_without_a_tenant_wait_and_other_events_are_skipped(self):
        unmapped = self.insert(callback(message("1700000200.000100")), tenant=None, event_id="Ev1")
        reaction = self.insert(callback({"type": "reaction_added", "user": "U0ADA", "reaction": "eyes",
                                         "item": {"type": "message", "channel": CHANNEL, "ts": "1"},
                                         "event_ts": "1700000600.000100"}), event_id="Ev2")

        async def scenario():
            worker = self.worker(FakeSlackApi())
            task = await self.started(worker)
            await self.until(lambda: self.raw(reaction).enriched_at is not None)
            await asyncio.sleep(0.3)
            await self.stopped(worker, task)

        run_async(scenario())
        self.assertIsNone(self.raw(unmapped).enriched_at)
        self.assertEqual(self.records(), [])

    def test_seed_stores_a_fixture_as_intake_would(self):
        added, duplicate, ignored = enrich.seed(
            self.url, str(REPO / "packages" / "ingestion-envelope" / "fixtures" / "slack.json"), "dev"
        )
        self.assertEqual((added, duplicate, ignored), (1, 0, 0))
        with Session(self.engine) as session:
            row = session.scalars(select(RawEvent)).one()
        self.assertEqual(
            (row.source, row.delivery_id, row.account_id, row.tenant, row.event_name, row.origin),
            ("slack", "Ev012EVENT", "T012WORKSPACE", "dev", "message", "webhook"),
        )
        self.assertEqual(row.envelope["type"], "slack")
        self.assertEqual(row.occurred_at, enrich.ordering_value(row.envelope["body"]))  # the source's time, not the insert


if __name__ == "__main__":
    unittest.main()
