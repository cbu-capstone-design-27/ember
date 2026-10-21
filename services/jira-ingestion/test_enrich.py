"""enrich:jira against a real Postgres built by the repo migrations (EMBER-66). Jira is stubbed.

Needs dbmate, SQLAlchemy and asyncpg (requirements.txt), and a Postgres server it may create and
drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/jira-ingestion/test_enrich.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from sqlalchemy import text  # noqa: E402

import enrich  # noqa: E402
from pull_test import Response  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
TEST_DB = "ember_test_enrich"
SITE = "photon-synthetic.atlassian.net"
ORIGIN = f"https://{SITE}"
TENANT = "photon"


def _photon() -> tuple[dict[str, dict], list[dict]]:
    """Issues by id and comments from the EMBER-43 synthetic pull."""
    issues, comments = {}, []
    with open(REPO / "services" / "pipeline" / "fixtures" / "photon" / "jira-intake.jsonl") as lines:
        for line in lines:
            body = json.loads(line)["body"]
            if {"expand", "fields", "key"} <= body.keys():
                issues[body["id"]] = body
            elif "updateAuthor" in body:
                comments.append(body)
    return issues, comments


ISSUES, COMMENTS = _photon()
UPDATED_FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "jira.json").read_text())["body"]


def issue_created(issue: dict) -> dict:
    return {"timestamp": 1757818271000, "webhookEvent": "jira:issue_created",
            "issue_event_type_name": "issue_created", "user": issue["fields"]["reporter"], "issue": issue}


def comment_created(comment: dict) -> dict:
    """A comment webhook: the comment plus the reduced issue Jira sends with it."""
    issue = ISSUES[enrich.COMMENT_ISSUE.search(comment["self"]).group(1)]
    reduced = {"id": issue["id"], "key": issue["key"], "self": issue["self"],
               "fields": {"summary": issue["fields"]["summary"]}}
    return {"timestamp": 1757818572000, "webhookEvent": "comment_created", "comment": comment, "issue": reduced}


def comment_on_new_issue(used: set[str]) -> dict:
    for comment in COMMENTS:
        issue_id = enrich.COMMENT_ISSUE.search(comment["self"]).group(1)
        if issue_id not in used:
            used.add(issue_id)
            return comment
    raise AssertionError("no comment on an unused issue")


class StubJira:
    """pull_test's exchange boundary: serves photon issues, after optional canned responses."""

    def __init__(self, *, delay: float = 0.0, first: list[Response] | None = None) -> None:
        self.delay = delay
        self.first = list(first or [])
        self.calls: list[tuple[float, str]] = []
        self.called = threading.Event()

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        self.calls.append((time.monotonic(), url))
        self.called.set()
        time.sleep(self.delay)
        if self.first:
            return self.first.pop(0)
        issue = ISSUES.get(re.search(r"/issue/(\d+)\?", url).group(1))
        if issue is None:
            return Response(404, {}, b"{}")
        return Response(200, {"X-RateLimit-Remaining": "99"}, json.dumps(issue).encode())


def run(*args: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed:\n{result.stdout}{result.stderr}")


def test_db_url() -> str:
    return urlunsplit(urlsplit(os.environ["PGTEST_URL"])._replace(path="/" + TEST_DB))


class EnrichTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = test_db_url()
        dbmate = [DBMATE, "--url", cls.url, "--migrations-dir", str(MIGRATIONS), "--no-dump-schema"]
        run(*dbmate, "drop")
        run(*dbmate, "up")

    @classmethod
    def tearDownClass(cls):
        run(DBMATE, "--url", cls.url, "drop")

    async def asyncSetUp(self):
        self.engine = enrich.make_engine(self.url, pool_size=10)
        async with self.engine.begin() as conn:
            await conn.execute(text("truncate raw_events, source_records, rate_limits restart identity cascade"))

    async def asyncTearDown(self):
        await self.engine.dispose()

    def worker(self, stub: StubJira | None = None) -> enrich.Worker:
        return enrich.Worker(self.engine, origin=ORIGIN, email="dev@example.com", api_token="placeholder",
                             exchange=stub or StubJira(), concurrency=4)

    async def insert(self, body: dict, delivery_id: str, account_id: str = SITE) -> int:
        async with self.engine.begin() as conn:
            return await conn.scalar(
                text("insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
                     " values ('jira', :d, :a, :t, :e, 'webhook', cast(:env as jsonb)) returning id"),
                {"d": delivery_id, "a": account_id, "t": TENANT, "e": body["webhookEvent"],
                 "env": json.dumps({"type": "jira", "body": body})},
            )

    async def scalar(self, sql: str, **params):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(sql), params)

    async def wait_for(self, sql: str, timeout: float = 5.0, **params) -> float:
        """Seconds until sql returns true."""
        start = time.monotonic()
        while not await self.scalar(sql, **params):
            if time.monotonic() - start > timeout:
                raise AssertionError(f"timed out waiting for: {sql}")
            await asyncio.sleep(0.02)
        return time.monotonic() - start

    async def run_until_enriched(self, worker: enrich.Worker) -> None:
        task = asyncio.create_task(worker.run())
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")
        worker.stop()
        await asyncio.wait_for(task, 10)

    async def test_issue_and_comment_events_become_source_records(self):
        issue = ISSUES["10001"]
        comment = COMMENTS[0]
        stub = StubJira()
        await self.insert(issue_created(issue), f"{SITE}:created")
        await self.insert(UPDATED_FIXTURE, "ember-capstone.atlassian.net:updated", "ember-capstone.atlassian.net")
        await self.insert(comment_created(comment), f"{SITE}:comment")
        await self.run_until_enriched(self.worker(stub))

        async with self.engine.connect() as conn:
            rows = {r.object_id: r for r in await conn.execute(text(
                "select kind, object_id, version, occurred_at, record from source_records"))}
        self.assertEqual(set(rows), {"10001", "10204", "10015/comment/10136"})

        created = rows["10001"]
        self.assertEqual((created.kind, created.version), ("issue", issue["fields"]["updated"]))
        self.assertEqual(created.occurred_at, datetime.strptime(issue["fields"]["updated"], enrich.JIRA_TIME))
        self.assertEqual(created.record["payload"]["issue"], issue)

        updated = rows["10204"]
        self.assertEqual(updated.kind, "issue")
        self.assertEqual(updated.version, str(UPDATED_FIXTURE["timestamp"]))   # the fixture has no fields.updated
        self.assertEqual(updated.record["payload"]["changelog"], UPDATED_FIXTURE["changelog"])

        commented = rows["10015/comment/10136"]
        self.assertEqual((commented.kind, commented.version), ("comment", comment["updated"]))
        self.assertEqual(commented.record["issues"]["10015"], ISSUES["10015"])
        self.assertEqual([url for _, url in stub.calls], [f"{ORIGIN}/rest/api/3/issue/10015?fields=%2Aall"])

    async def test_reprocessing_and_duplicate_versions_leave_one_record(self):
        body = issue_created(ISSUES["10001"])
        await self.insert(body, f"{SITE}:first")
        await self.run_until_enriched(self.worker())

        async with self.engine.begin() as conn:
            await conn.execute(text("update raw_events set enriched_at = null"))
        await self.insert(body, f"{SITE}:retry-with-a-new-id")
        await self.run_until_enriched(self.worker())

        self.assertEqual(await self.scalar("select count(*) from source_records"), 1)

    async def test_wakes_on_insert_and_catches_up_on_start(self):
        before = await self.insert(issue_created(ISSUES["10001"]), f"{SITE}:before-start")
        worker = self.worker()
        task = asyncio.create_task(worker.run())
        await self.wait_for("select enriched_at is not null from raw_events where id = :id", id=before)

        after = await self.insert(issue_created(ISSUES["10002"]), f"{SITE}:while-running")
        elapsed = await self.wait_for("select exists (select 1 from source_records where raw_event_id = :id)", id=after)
        worker.stop()
        await asyncio.wait_for(task, 10)
        self.assertLess(elapsed, 1.0)

    async def test_sigint_finishes_the_row_in_flight(self):
        stub = StubJira(delay=1.0)
        row = await self.insert(comment_created(COMMENTS[0]), f"{SITE}:slow")
        task = asyncio.create_task(self.worker(stub).run())
        await asyncio.to_thread(stub.called.wait, 5)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(task, 10)

        # The fetch ends at least stub.delay after the row was received, and enriched_at can't be earlier
        received_to_enriched = await self.scalar(
            "select extract(epoch from enriched_at - received_at) from raw_events where id = :id", id=row)
        self.assertGreaterEqual(received_to_enriched, stub.delay)
        self.assertEqual(await self.scalar("select count(*) from source_records where raw_event_id = :id", id=row), 1)

    async def test_retry_after_sets_next_allowed_at_and_calls_wait_for_it(self):
        used: set[str] = set()
        stub = StubJira(first=[Response(429, {"Retry-After": "1"}, b"{}")])
        row = await self.insert(comment_created(comment_on_new_issue(used)), f"{SITE}:throttled")
        await self.run_until_enriched(self.worker(stub))

        (throttled_at, _), (retried_at, _) = stub.calls
        self.assertGreaterEqual(retried_at - throttled_at, 1.0)
        # The 429 came after the row was received, so next_allowed_at is at least 1 s past that
        pushed = await self.scalar(
            "select extract(epoch from l.next_allowed_at - r.received_at) from rate_limits l, raw_events r"
            " where l.source = 'jira' and l.tenant = :t and l.bucket = :b and r.id = :id",
            t=TENANT, b=ORIGIN, id=row)
        self.assertGreaterEqual(pushed, 1.0)

        # The next call waits for next_allowed_at even with no 429 of its own
        async with self.engine.begin() as conn:
            await conn.execute(text("update rate_limits set next_allowed_at = now() + interval '1.5 seconds'"))
        held_at = time.monotonic()
        stub = StubJira()
        await self.insert(comment_created(comment_on_new_issue(used)), f"{SITE}:held")
        await self.run_until_enriched(self.worker(stub))
        self.assertGreaterEqual(stub.calls[0][0] - held_at, 1.4)


if __name__ == "__main__":
    unittest.main()
