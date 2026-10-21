"""enrich:github against a real Postgres (EMBER-65). The GitHub API is stubbed at the exchange boundary.

Needs psql, dbmate, the packages in requirements.txt, and a Postgres server it may create and drop
a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/github-ingestion/test_enrich.py

It builds the repo migrations in database ember_test_enrich.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import enrich  # noqa: E402
from models import RateLimit, RawEvent, SourceRecord  # noqa: E402
from pull_test import Response  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_enrich"
FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "github.json").read_text())
INSTALLATION = {"id": 77}
REPOSITORY = FIXTURE["body"]["repository"]


def database_url() -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def issue_opened() -> dict:
    return {**FIXTURE["body"], "installation": INSTALLATION}


def pull_request_opened() -> dict:
    pull_request = {
        "id": 20043, "number": 43, "title": "Keep the query string on login redirect", "state": "open",
        "body": "Fixes #42", "head": {"ref": "fix-redirect", "sha": "c" * 40}, "base": {"ref": "main", "sha": "d" * 40},
        "created_at": "2026-09-24T10:00:00Z", "updated_at": "2026-09-24T10:00:00Z",
    }
    return {"action": "opened", "pull_request": pull_request, "repository": REPOSITORY,
            "sender": FIXTURE["body"]["sender"], "installation": INSTALLATION}


def push(commit_count: int) -> dict:
    commits = [{"id": f"{n:040x}", "message": f"commit {n}", "timestamp": "2026-09-25T09:00:00Z"} for n in range(commit_count)]
    return {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40, "commits": commits,
            "repository": {**REPOSITORY, "pushed_at": 1790326800}, "installation": INSTALLATION}


def ok(data) -> Response:
    return Response(200, {"x-ratelimit-remaining": "4999"}, json.dumps(data).encode())


class FakeGitHub:
    """The exchange enrich calls. Records every call; `slow` and `throttle` shape the files call."""

    FILES = [{"filename": "app/login.py", "status": "modified", "additions": 3, "deletions": 1}]
    COMPARE = [{"sha": f"{n:040x}", "commit": {"message": f"compared {n}"}} for n in range(3)]

    def __init__(self, *, slow: float = 0.0, throttle: bool = False) -> None:
        self.slow = slow
        self.throttle = throttle
        self.calls: list[tuple[float, str]] = []
        self.in_files = threading.Event()

    def paths(self) -> list[str]:
        return [urlsplit(url).path for _at, url in self.calls]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        self.calls.append((time.time(), url))
        path = urlsplit(url).path
        if path.endswith("/installation"):
            return ok(INSTALLATION)
        if path.endswith("/access_tokens"):
            return ok({"token": "installation-token"})
        if "/compare/" in path:
            return ok({"commits": self.COMPARE})
        if path.endswith("/files"):
            self.in_files.set()
            time.sleep(self.slow)
            if self.throttle:
                self.throttle = False
                return Response(429, {"Retry-After": "2"}, b'{"message": "slow down"}')
            return ok(self.FILES)
        raise AssertionError(f"unexpected call {method} {url}")


class EnrichTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
            "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
        run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")

    @classmethod
    def tearDownClass(cls):
        run("psql", "-X", "-q", "-d", os.environ["PGTEST_URL"], "-c", f"drop database if exists {DB} with (force)")

    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, source_records, episodes, rate_limits restart identity")

    async def asyncTearDown(self):
        await self.db.close()

    async def insert(self, event_name: str, body: dict, delivery_id: str | None = None) -> int:
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
            " values ('github', $1, '77', 'acme', $2, 'webhook', $3::jsonb) returning id",
            delivery_id or f"delivery-{time.monotonic_ns()}", event_name, json.dumps({"type": "github", "body": body}),
        )

    def start(self, github: FakeGitHub) -> tuple[enrich.Worker, asyncio.Task]:
        worker = enrich.Worker(database_url(), exchange=github, app_jwt=lambda: "app-jwt")
        return worker, asyncio.create_task(worker.run())

    async def wait_enriched(self, *ids: int, timeout: float = 10.0) -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            waiting = await self.db.fetchval(
                "select count(*) from raw_events where id = any($1::bigint[]) and enriched_at is null", list(ids))
            if waiting == 0:
                return time.monotonic() - started
            await asyncio.sleep(0.02)
        raise AssertionError(f"raw_events {ids} not enriched within {timeout}s")

    async def process(self, github: FakeGitHub, *ids: int) -> None:
        worker, task = self.start(github)
        await self.wait_enriched(*ids)
        worker.stop()
        await task

    async def records(self):
        return await self.db.fetch("select * from source_records order by kind, object_id")

    async def test_models_match_the_migrated_schema(self):
        for model in (RawEvent, SourceRecord, RateLimit):
            table = model.__table__
            rows = await self.db.fetch(
                "select column_name, is_nullable = 'YES' as nullable from information_schema.columns"
                " where table_schema = 'public' and table_name = $1", table.name)
            self.assertEqual({(c.name, c.nullable) for c in table.columns},
                             {(r["column_name"], r["nullable"]) for r in rows}, table.name)

    async def test_issue_and_pull_request_become_source_records(self):
        github = FakeGitHub()
        ids = [await self.insert("issues:opened", issue_opened()),
               await self.insert("pull_request:opened", pull_request_opened())]
        await self.process(github, *ids)

        issue, pull = await self.records()
        self.assertEqual((issue["kind"], issue["object_id"], issue["version"]),
                         ("issue", "acme/widget#42", "2026-09-23T22:15:00Z"))
        self.assertEqual(issue["occurred_at"].isoformat(), "2026-09-23T22:15:00+00:00")
        self.assertEqual(json.loads(issue["record"]), {"payload": issue_opened()})
        self.assertEqual((pull["kind"], pull["object_id"], pull["version"], pull["tenant"]),
                         ("pull_request", "acme/widget#43", "2026-09-24T10:00:00Z", "acme"))
        self.assertEqual(json.loads(pull["record"])["fetched"], {"files": FakeGitHub.FILES})
        self.assertEqual(github.paths()[-1], "/repos/acme/widget/pulls/43/files")

    async def test_full_push_uses_compare_and_small_push_does_not(self):
        small = FakeGitHub()
        await self.process(small, await self.insert("push", push(2)))
        self.assertEqual(small.calls, [])
        commits = [r for r in await self.records() if r["kind"] == "commit"]
        self.assertEqual(len(commits), 2)

        await self.db.execute("truncate source_records, episodes")
        full = FakeGitHub()
        await self.process(full, await self.insert("push", push(enrich.FULL_PUSH)))
        self.assertIn(f"/repos/acme/widget/compare/{'a' * 40}...{'b' * 40}", full.paths())
        rows = await self.records()
        self.assertEqual(sorted(r["version"] for r in rows if r["kind"] == "commit"),
                         [c["sha"] for c in FakeGitHub.COMPARE])
        (ref,) = [r for r in rows if r["kind"] == "ref"]
        self.assertEqual((ref["object_id"], ref["version"]), ("acme/widget:refs/heads/main", "b" * 40))
        self.assertEqual(ref["occurred_at"].timestamp(), 1790326800)

    async def test_installation_events_are_enriched_without_a_record_or_a_call(self):
        # No `repository` on these (github-webhooks.md, "Event types"); they used to fail on every poll
        github = FakeGitHub()
        ids = [await self.insert("installation:new_permissions_accepted",
                                 {"action": "new_permissions_accepted", "installation": INSTALLATION}),
               await self.insert("installation_repositories:added",
                                 {"action": "added", "installation": INSTALLATION, "repositories_added": [REPOSITORY]})]
        await self.process(github, *ids)
        self.assertEqual(await self.records(), [])
        self.assertEqual(github.calls, [])

    async def test_reprocessing_and_duplicate_versions_leave_one_record(self):
        first = await self.insert("issues:opened", issue_opened())
        await self.process(FakeGitHub(), first)
        await self.db.execute("update raw_events set enriched_at = null")
        second = await self.insert("issues:opened", issue_opened(), delivery_id="redelivered-by-reconcile")
        await self.process(FakeGitHub(), first, second)
        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), 1)

    async def test_catches_up_at_start_and_wakes_on_insert_within_a_second(self):
        before = await self.insert("issues:opened", issue_opened())
        worker, task = self.start(FakeGitHub())
        try:
            await self.wait_enriched(before, timeout=5)
            after = await self.insert("pull_request:edited", pull_request_opened())
            self.assertLess(await self.wait_enriched(after, timeout=5), 1.0)
        finally:
            worker.stop()
            await task

    async def test_sigint_finishes_the_row_in_flight_then_exits(self):
        github = FakeGitHub(slow=1.0)
        row = await self.insert("pull_request:opened", pull_request_opened())
        _worker, task = self.start(github)
        self.assertTrue(await asyncio.to_thread(github.in_files.wait, 5))
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(task, 5)
        enriched_at = await self.db.fetchval("select enriched_at from raw_events where id = $1", row)
        files_called_at = [at for at, url in github.calls if "/files" in url][0]
        self.assertGreaterEqual(enriched_at.timestamp(), files_called_at + github.slow - 0.1, "enriched_at predates the fetch")
        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), 1)

    async def test_burst_skips_ci_events_in_bulk_and_fetches_each_head_once(self):
        ci = [(f"{event}:completed", {"action": "completed", event: {"id": n}, "repository": REPOSITORY,
                                      "installation": INSTALLATION})
              for n, event in enumerate(["workflow_job", "check_run"] * 150)]
        await self.db.executemany(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
            " values ('github', $1, '77', 'acme', $2, 'webhook', $3::jsonb)",
            [(f"ci-{n}", name, json.dumps({"type": "github", "body": body})) for n, (name, body) in enumerate(ci)])
        pulls = []
        for n in range(6):
            body = pull_request_opened() | {"action": "synchronize"}
            body["pull_request"] = body["pull_request"] | {
                "head": {"ref": "fix-redirect", "sha": ("e" if n < 3 else "f") * 40},
                "updated_at": f"2026-09-24T10:0{n}:00Z"}
            pulls.append(await self.insert("pull_request:synchronize", body))
        issue = await self.insert("issues:opened", issue_opened())

        github = FakeGitHub()
        await self.process(github, *range(1, 301), *pulls, issue)

        self.assertEqual(sum(path.endswith("/files") for path in github.paths()), 2)
        rows = await self.records()
        self.assertEqual(sorted((r["kind"], r["version"]) for r in rows),
                         [("issue", "2026-09-23T22:15:00Z")]
                         + [("pull_request", f"2026-09-24T10:0{n}:00Z") for n in range(6)])
        self.assertTrue(all(json.loads(r["record"])["fetched"] == {"files": FakeGitHub.FILES}
                            for r in rows if r["kind"] == "pull_request"))
        # Rows one statement updates share its transaction's xmin. Per-row transactions would give 300
        self.assertEqual(await self.db.fetchval(
            "select count(distinct xmin::text) from raw_events where id = any($1::bigint[])", list(range(1, 301))), 1)

    async def test_retry_after_is_stored_and_the_next_call_waits_for_it(self):
        github = FakeGitHub(throttle=True)
        await self.process(github, await self.insert("pull_request:opened", pull_request_opened()))
        limit = await self.db.fetchrow("select * from rate_limits where source = 'github' and tenant = 'acme' and bucket = '77'")
        throttled_at, retried_at = [at for at, url in github.calls if url.split("?")[0].endswith("/files")]
        next_allowed = limit["next_allowed_at"].timestamp()
        self.assertAlmostEqual(next_allowed - throttled_at, 2.0, delta=0.5)
        self.assertGreaterEqual(retried_at, next_allowed - 0.1)


if __name__ == "__main__":
    unittest.main()
