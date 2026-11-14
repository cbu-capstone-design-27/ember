"""The runtime (EMBER-160): the supervisor, the queue-worker base and the controller, against a real Postgres.

Needs psql, dbmate, this package installed (`pip install services/backend`), and a Postgres server it
may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \\
        python3 services/backend/tests/test_runtime.py

It builds the repo migrations in database ember_test_runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from prometheus_client import CollectorRegistry
from sqlalchemy import select

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

from ember.connectors.base import Connector  # noqa: E402
from ember.connectors.github import GitHubConnector  # noqa: E402
from ember.core.db import Database  # noqa: E402
from ember.core.errors import ConfigError, Permanent, Throttled, Transient  # noqa: E402
from ember.core.models import RawEvent  # noqa: E402
from ember.core.settings import ConnectorSettings, Settings  # noqa: E402
from ember.runtime.component import Component  # noqa: E402
from ember.runtime.controller import Controller  # noqa: E402
from ember.runtime.supervisor import State, Supervisor  # noqa: E402
from ember.work.queue_worker import QueueWorker  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_runtime"
SECRET = "test-webhook-secret"
T0 = datetime(2026, 11, 14, tzinfo=timezone.utc)


def database_url() -> str:
    return urlunsplit(urlsplit(os.environ["PGTEST_URL"])._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def setUpModule():
    run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
        "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
    run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")


def tearDownModule():
    run("psql", "-X", "-q", "-d", os.environ["PGTEST_URL"], "-c", f"drop database if exists {DB} with (force)")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


async def eventually(check, timeout: float = 5.0):
    """Wait until check() returns something truthy, and return it."""
    deadline = time.monotonic() + timeout
    while True:
        result = await check() if asyncio.iscoroutinefunction(check) else check()
        if result or time.monotonic() > deadline:
            return result
        await asyncio.sleep(0.02)


# ---------- supervisor ----------

class Steady(Component):
    def __init__(self, name: str = "steady") -> None:
        self.name = name
        self.closed = 0
        self.stopped_at: float | None = None

    async def run(self, stopping):
        await stopping.wait()
        self.stopped_at = time.monotonic()

    async def close(self):
        self.closed += 1


class Crashing(Component):
    """Crashes `times` times (or forever), in start or in run."""

    def __init__(self, name: str = "crashing", times: int | None = None, where: str = "run") -> None:
        self.name = name
        self.times = times
        self.where = where
        self.attempts = 0
        self.closed = 0

    def _maybe_crash(self):
        if self.times is None or self.attempts <= self.times:
            raise RuntimeError(f"boom {self.attempts}")

    async def start(self):
        self.attempts += 1
        if self.where == "start":
            self._maybe_crash()

    async def run(self, stopping):
        if self.where == "run":
            self._maybe_crash()
        await stopping.wait()

    async def close(self):
        self.closed += 1


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    def supervisor(self, *components, **kwargs) -> Supervisor:
        return Supervisor(components, CollectorRegistry(), **{"restart_max": 0.01, **kwargs})

    async def test_a_crashing_component_is_restarted_and_the_others_keep_running(self):
        steady, crashing = Steady(), Crashing(times=3)
        supervisor = self.supervisor(steady, crashing)
        stopping = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stopping))
        self.assertTrue(await eventually(lambda: supervisor.state("crashing") is State.RUNNING and crashing.attempts == 4))
        self.assertEqual(supervisor.state("steady"), State.RUNNING)
        self.assertEqual(steady.closed, 0)  # never restarted
        self.assertEqual(crashing.closed, 3)  # close runs after every crashed run
        status = supervisor.status()["crashing"]
        self.assertEqual((status["state"], status["restarts"]), ("running", 3))
        stopping.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(supervisor.state("steady"), State.STOPPED)
        self.assertEqual((crashing.closed, steady.closed), (4, 1))

    async def test_a_component_that_crashes_on_start_backs_off_and_reports_why(self):
        crashing = Crashing(where="start")
        supervisor = self.supervisor(Steady(), crashing, restart_max=60)
        stopping = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stopping))
        # 1 s, then 2 s: after ~1.5 s it has tried twice and waits
        await asyncio.sleep(1.5)
        self.assertEqual(crashing.attempts, 2)
        status = supervisor.status()["crashing"]
        self.assertEqual(status["state"], "backoff")
        self.assertEqual(status["reason"], "RuntimeError: boom 2")
        stopping.set()  # a component in backoff stops at once
        await asyncio.wait_for(task, 2)
        self.assertEqual(supervisor.state("crashing"), State.STOPPED)

    async def delays(self, crashing: Crashing, **kwargs) -> list[float]:
        """The waits between restarts, without waiting them."""
        delays = []

        async def recording_wait_for(awaitable, timeout):
            delays.append(timeout)
            awaitable.close()
            raise TimeoutError

        supervisor = Supervisor([crashing], CollectorRegistry(), **kwargs)
        stopping = asyncio.Event()
        with mock.patch("ember.runtime.supervisor.asyncio.wait_for", recording_wait_for):
            task = asyncio.create_task(supervisor.run(stopping))
            await eventually(lambda: supervisor.state("crashing") is State.RUNNING)
        stopping.set()
        await asyncio.wait_for(task, 5)
        return delays

    async def test_backoff_doubles_up_to_the_cap(self):
        self.assertEqual(await self.delays(Crashing(times=6), restart_max=8), [1, 2, 4, 8, 8, 8])

    async def test_backoff_starts_again_once_a_component_has_run_for_a_while(self):
        ticks = itertools.count(0, 301)  # every crash comes 301 s after its start: past healthy_after
        self.assertEqual(await self.delays(Crashing(times=3), restart_max=8, healthy_after=300,
                                           clock=lambda: next(ticks)), [1, 1, 1])

    async def test_a_config_error_disables_the_component_for_good(self):
        class Misconfigured(Component):
            name = "misconfigured"
            starts = 0

            async def start(self):
                self.starts += 1
                raise ConfigError("SOME_TOKEN is not set")

            async def run(self, stopping):
                raise AssertionError("never runs")

        misconfigured = Misconfigured()
        supervisor = self.supervisor(Steady(), misconfigured)
        stopping = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stopping))
        self.assertTrue(await eventually(lambda: supervisor.state("misconfigured") is State.DISABLED))
        await asyncio.sleep(0.1)
        self.assertEqual(misconfigured.starts, 1)
        self.assertEqual(supervisor.status()["misconfigured"]["reason"], "SOME_TOKEN is not set")
        self.assertEqual(supervisor.state("steady"), State.RUNNING)
        stopping.set()
        await asyncio.wait_for(task, 5)

    async def test_returning_before_shutdown_counts_as_a_crash(self):
        class Quitter(Component):
            name = "quitter"
            runs = 0

            async def run(self, stopping):
                self.runs += 1

        quitter = Quitter()
        supervisor = self.supervisor(quitter)
        stopping = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stopping))
        self.assertTrue(await eventually(lambda: quitter.runs >= 3))
        self.assertEqual(supervisor.status()["quitter"]["reason"], "returned before shutdown")
        stopping.set()
        await asyncio.wait_for(task, 5)

    async def test_ingress_stops_first_and_the_rest_after_it(self):
        class Ingress(Steady):
            stops_first = True

            async def run(self, stopping):
                await stopping.wait()
                await asyncio.sleep(0.2)  # draining requests in flight
                self.stopped_at = time.monotonic()

        ingress, worker = Ingress("http"), Steady("worker")
        supervisor = self.supervisor(worker, ingress)
        stopping = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stopping))
        await eventually(lambda: supervisor.state("worker") is State.RUNNING)
        stopping.set()
        await asyncio.wait_for(task, 5)
        self.assertGreater(worker.stopped_at, ingress.stopped_at)

    async def test_a_component_still_running_after_the_grace_period_is_cancelled(self):
        class Stubborn(Steady):
            async def run(self, stopping):
                await stopping.wait()
                await asyncio.sleep(60)

        stubborn = Stubborn("stubborn")
        supervisor = self.supervisor(stubborn, grace=0.2)
        stopping = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stopping))
        await eventually(lambda: supervisor.state("stubborn") is State.RUNNING)
        started = time.monotonic()
        with self.assertLogs("ember.supervisor", "WARNING") as logs:
            stopping.set()
            await asyncio.wait_for(task, 5)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("stubborn did not stop within", "\n".join(logs.output))
        self.assertEqual(stubborn.closed, 1)  # close still ran


# ---------- queue worker ----------

class Marker(QueueWorker[RawEvent]):
    """Marks `test` raw_events enriched, recording the order; a row's envelope can ask it to fail or be slow."""

    channel = "raw_events"
    model = RawEvent

    def __init__(self, db: Database, **kwargs) -> None:
        super().__init__(db, name="enrich:test", **kwargs)
        self.seen: list[str] = []
        self.failures: dict[str, list[Exception]] = {}
        self.in_flight = 0
        self.most_in_flight = 0

    def pending(self):
        return (select(RawEvent).where(RawEvent.source == "test", RawEvent.enriched_at.is_(None))
                .order_by(RawEvent.occurred_at, RawEvent.id))

    async def process(self, session, row):
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            self.seen.append(row.delivery_id)
            await asyncio.sleep(row.envelope["body"].get("sleep", 0))
            queued = self.failures.get(row.delivery_id)
            if queued:
                raise queued.pop(0)
            row.enriched_at = datetime.now(timezone.utc)
        finally:
            self.in_flight -= 1


class FailingClaim(Marker):
    """Its second job fails once the first is in the middle of a row, as a claim that times out would."""

    def __init__(self, db: Database, **kwargs) -> None:
        super().__init__(db, **kwargs)
        self.calls = 0

    async def job(self) -> bool:
        self.calls += 1
        if self.calls == 2:
            while not self.in_flight:
                await asyncio.sleep(0.01)
            raise RuntimeError("claim failed")
        return await super().job()


class QueueWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events restart identity cascade")
        self.tasks: list[tuple[asyncio.Event, asyncio.Task]] = []

    async def asyncTearDown(self):
        for stopping, task in self.tasks:
            stopping.set()
            await asyncio.wait_for(task, 10)
        await self.db.close()

    async def insert(self, delivery_id: str, occurred_at: datetime | None = None, **body) -> None:
        await self.db.execute(
            "insert into raw_events (source, delivery_id, event_name, origin, envelope, occurred_at)"
            " values ('test', $1, 'thing', 'webhook', $2::jsonb, $3)",
            delivery_id, json.dumps({"type": "test", "body": body}), occurred_at or T0)

    async def unenriched(self) -> list[str]:
        return [r["delivery_id"] for r in await self.db.fetch(
            "select delivery_id from raw_events where enriched_at is null order by id")]

    async def start(self, worker: Marker) -> asyncio.Event:
        stopping = asyncio.Event()

        async def run():
            await worker.start()
            try:
                await worker.run(stopping)
            finally:
                await worker.close()
        self.tasks.append((stopping, asyncio.create_task(run())))
        return stopping

    def worker(self, **kwargs) -> Marker:
        return Marker(Database(database_url()), **{"poll_seconds": 60, **kwargs})

    async def test_catches_up_on_start_in_the_sources_order_then_wakes_on_insert(self):
        await self.insert("later", T0 + timedelta(minutes=1))
        await self.insert("earlier", T0)
        await self.insert("same-time-first-id", T0 + timedelta(minutes=1))
        worker = self.worker(concurrency=1)
        await self.start(worker)
        self.assertEqual(await eventually(self.unenriched_empty), True)
        # occurred_at, then id: arrival order is not the source's order (EMBER-63)
        self.assertEqual(worker.seen, ["earlier", "later", "same-time-first-id"])
        # The poll is a minute away: only the insert's NOTIFY can wake it
        started = time.monotonic()
        await self.insert("new")
        self.assertTrue(await eventually(lambda: "new" in worker.seen, timeout=2))
        self.assertLess(time.monotonic() - started, 1)

    async def unenriched_empty(self) -> bool:
        return not await self.unenriched()

    async def test_runs_up_to_concurrency_jobs_at_once(self):
        for n in range(6):
            await self.insert(f"slow-{n}", sleep=0.3)
        worker = self.worker(concurrency=3)
        started = time.monotonic()
        await self.start(worker)
        self.assertTrue(await eventually(self.unenriched_empty))
        self.assertEqual(worker.most_in_flight, 3)
        self.assertLess(time.monotonic() - started, 1.5)  # two rounds of 0.3 s, not six

    async def test_replicas_never_take_the_same_row(self):
        for n in range(20):
            await self.insert(f"row-{n}", sleep=0.02)
        workers = [self.worker(concurrency=2, batch=3) for _ in range(2)]
        for worker in workers:
            await self.start(worker)
        self.assertTrue(await eventually(self.unenriched_empty))
        seen = workers[0].seen + workers[1].seen
        self.assertEqual(sorted(seen), sorted(f"row-{n}" for n in range(20)))
        self.assertTrue(workers[0].seen and workers[1].seen)

    async def test_a_failed_row_is_set_aside_and_the_rest_still_go(self):
        now = [0.0]
        worker = self.worker(concurrency=1, batch=5, clock=lambda: now[0], poll_seconds=0.05)
        worker.failures = {"transient": [Transient("503"), Transient("503")], "throttled": [Throttled(retry_after=30)],
                           "permanent": [Permanent("bad payload")], "bug": [KeyError("x")]}
        for name in ("transient", "throttled", "permanent", "bug", "fine"):
            await self.insert(name)
        with self.assertLogs("ember.work", "WARNING") as logs:
            await self.start(worker)
            self.assertTrue(await eventually(lambda: "fine" in worker.seen))
            await asyncio.sleep(0.2)  # several polls: nothing is retried before its time
            self.assertEqual(sorted(await self.unenriched()), ["bug", "permanent", "throttled", "transient"])
            self.assertEqual(worker.seen.count("transient"), 1)
            # A savepoint per row: the batch's good row committed despite the four failures
            now[0] += 2.1  # the first transient retry, and the unclassified error's, are due after 2 s
            self.assertTrue(await eventually(lambda: worker.seen.count("transient") == 2))
            self.assertTrue(await eventually(lambda: worker.seen.count("bug") == 2))
            now[0] += 4.1  # the second transient retry waits 4 s
            self.assertTrue(await eventually(lambda: worker.seen.count("transient") == 3))
            now[0] += 30  # throttled: after retry_after
            self.assertTrue(await eventually(lambda: worker.seen.count("throttled") == 2))
        self.assertEqual(await eventually(self.only_permanent_left), True)
        self.assertEqual(worker.seen.count("permanent"), 1)  # set aside for an hour
        self.assertIn("permanent, retrying in 3600s", "\n".join(logs.output))

    async def only_permanent_left(self) -> bool:
        return await self.unenriched() == ["permanent"]

    async def test_stopping_finishes_the_rows_in_hand_and_claims_no_more(self):
        for n in range(4):
            await self.insert(f"slow-{n}", sleep=0.5)
        worker = self.worker(concurrency=2)
        stopping = await self.start(worker)
        self.assertTrue(await eventually(lambda: worker.in_flight == 2))
        stopping.set()
        await asyncio.wait_for(self.tasks.pop()[1], 5)
        self.assertEqual(worker.seen, ["slow-0", "slow-1"])
        self.assertEqual(await self.unenriched(), ["slow-2", "slow-3"])

    async def test_a_job_that_fails_cancels_the_others(self):
        await self.insert("slow", sleep=0.5)
        worker = FailingClaim(Database(database_url()), concurrency=2, poll_seconds=60)
        await worker.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "claim failed"):
                await asyncio.wait_for(worker.run(asyncio.Event()), 5)
            # The other job is cancelled with it, not left running on into the restart
            self.assertEqual(worker.in_flight, 0)
        finally:
            await worker.close()
        self.assertEqual(await self.unenriched(), ["slow"])

    async def test_a_dropped_listen_connection_reconnects_and_catches_up(self):
        worker = self.worker(concurrency=1)
        await self.start(worker)
        await eventually(lambda: worker.processed == 0 and worker.engine is not None)
        await asyncio.sleep(0.3)
        killed = await self.db.fetchval(
            "select count(pg_terminate_backend(pid)) from pg_stat_activity where datname = $1 and query like 'LISTEN%'", DB)
        self.assertEqual(killed, 1)
        await self.insert("while-reconnecting")
        self.assertTrue(await eventually(lambda: "while-reconnecting" in worker.seen, timeout=5))


# ---------- controller ----------

class BrokenConnector(Connector):
    """A source whose webhook secret is unusable and whose worker crashes every time it runs."""

    name = "teams"  # type: ignore[assignment]
    Settings = ConnectorSettings

    def adapter(self):
        raise ValueError("TEAMS_CLIENT_STATE is required")

    def components(self):
        return [Crashing("enrich:teams")]


class ControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_broken_connector_does_not_take_down_the_rest(self):
        port = free_port()
        env = {"DATABASE_URL": database_url(), "LISTEN_PORT": str(port), "GITHUB_WEBHOOK_SECRET": SECRET,
               "EMBER_RESTART_MAX": "0.05"}
        with mock.patch.dict(os.environ, env):
            controller = Controller(Settings.load(), [GitHubConnector, BrokenConnector])
            task = asyncio.create_task(controller.run())

            async def status():
                try:
                    code, body = await asyncio.to_thread(request, f"http://127.0.0.1:{port}/status")
                except OSError:
                    return None
                report = json.loads(body)
                return report if report["components"]["enrich:teams"]["restarts"] >= 3 else None
            report = await eventually(status, timeout=10)
            self.assertIsNotNone(report)
            self.assertEqual(report["disabled"], {"webhook:teams": "TEAMS_CLIENT_STATE is required"})
            self.assertEqual(report["components"]["intake"]["state"], "running")
            self.assertIn("RuntimeError: boom", report["components"]["enrich:teams"]["reason"])

            payload = json.dumps({"action": "opened", "installation": {"id": 1}}).encode()
            headers = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "controller-1",
                       "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()}
            self.assertEqual(await asyncio.to_thread(request, f"http://127.0.0.1:{port}/webhook/github", payload, headers),
                             (200, '{"status":"stored"}'))
            self.assertEqual((await asyncio.to_thread(request, f"http://127.0.0.1:{port}/health"))[0], 200)

            controller.stop()
            self.assertEqual(await asyncio.wait_for(task, 10), 0)


if __name__ == "__main__":
    unittest.main()
