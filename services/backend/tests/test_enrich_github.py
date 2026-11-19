"""enrich:github (EMBER-65, moved in EMBER-160) against a real Postgres. GitHub is stubbed at the exchange.

Needs psql, dbmate, this package installed (`pip install services/backend`), and a Postgres server it
may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \\
        python3 services/backend/tests/test_enrich_github.py

It builds the repo migrations in database ember_test_enrich_github. The cases carried over from
services/github-ingestion/test_enrich.py keep their names.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

from ember.connectors import CONNECTORS  # noqa: E402
from ember.connectors.github import GitHubConnector, api  # noqa: E402
from ember.connectors.github.backfill import GitHubBackfill  # noqa: E402
from ember.connectors.github.enrich import FULL_PUSH, GitHubEnrich, missing_app  # noqa: E402
from ember.core.credentials import Credentials  # noqa: E402
from ember.core.db import Database  # noqa: E402
from ember.core.errors import ConfigError  # noqa: E402
from ember.core.models import RateLimit, RawEvent, SourceRecord  # noqa: E402
from ember.core.settings import Settings  # noqa: E402
from ember.runtime.controller import plan  # noqa: E402
from ember.runtime.deps import Deps  # noqa: E402

MIGRATIONS = REPO / "services" / "backend" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_enrich_github"
FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "github.json").read_text())
INSTALLATION = {"id": 77}
REPOSITORY = FIXTURE["body"]["repository"]
T0 = datetime(2026, 9, 23, 22, 15, tzinfo=timezone.utc)


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


def issue_opened() -> dict:
    return {**FIXTURE["body"], "installation": INSTALLATION}


def pull_request_opened(number: int = 43) -> dict:
    pull_request = {
        "id": 20000 + number, "number": number, "title": "Keep the query string on login redirect", "state": "open",
        "body": "Fixes #42", "head": {"ref": "fix-redirect", "sha": "c" * 40}, "base": {"ref": "main", "sha": "d" * 40},
        "created_at": "2026-09-24T10:00:00Z", "updated_at": "2026-09-24T10:00:00Z",
    }
    return {"action": "opened", "pull_request": pull_request, "repository": REPOSITORY,
            "sender": FIXTURE["body"]["sender"], "installation": INSTALLATION}


def push(commit_count: int) -> dict:
    commits = [{"id": f"{n:040x}", "message": f"commit {n}", "timestamp": "2026-09-25T09:00:00Z"} for n in range(commit_count)]
    return {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40, "commits": commits,
            "repository": {**REPOSITORY, "pushed_at": 1790326800}, "installation": INSTALLATION}


def ok(data) -> api.Response:
    return api.Response(200, {"x-ratelimit-remaining": "4999"}, json.dumps(data).encode())


class FakeGitHub:
    """The exchange enrich calls. Records every call; the options shape the files call."""

    FILES = [{"filename": "app/login.py", "status": "modified", "additions": 3, "deletions": 1}]
    COMPARE = [{"sha": f"{n:040x}", "commit": {"message": f"compared {n}"}} for n in range(3)]

    def __init__(self, *, slow: float = 0.0, retry_after: int | None = None, files_status: int = 200) -> None:
        self.slow = slow
        self.retry_after = retry_after  # the first files call answers 429 with this Retry-After
        self.files_status = files_status
        self.calls: list[tuple[float, str]] = []
        self.in_files = threading.Event()

    def paths(self) -> list[str]:
        return [urlsplit(url).path for _at, url in self.calls]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> api.Response:
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
            if self.retry_after is not None:
                wait, self.retry_after = self.retry_after, None
                return api.Response(429, {"Retry-After": str(wait)}, b'{"message": "slow down"}')
            if self.files_status != 200:
                return api.Response(self.files_status, {}, b'{"message": "Not Found"}')
            return ok(self.FILES)
        raise AssertionError(f"unexpected call {method} {url}")


class EnrichTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, source_records, episodes, rate_limits restart identity cascade")
        self.running: list[tuple[GitHubEnrich, asyncio.Event, asyncio.Task]] = []

    async def asyncTearDown(self):
        for _worker, stopping, task in self.running:
            stopping.set()
            await asyncio.wait_for(task, 10)
        await self.db.close()

    async def insert(self, event_name: str, body: dict, delivery_id: str | None = None,
                     occurred_at: datetime = T0, tenant: str | None = "acme") -> int:
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope, occurred_at)"
            " values ('github', $1, '77', $2, $3, 'webhook', $4::jsonb, $5) returning id",
            delivery_id or f"delivery-{time.monotonic_ns()}", tenant, event_name,
            json.dumps({"type": "github", "body": body}), occurred_at)

    def start(self, github: FakeGitHub, **kwargs) -> tuple[GitHubEnrich, asyncio.Event, asyncio.Task]:
        worker = GitHubEnrich(Database(database_url()), exchange=github,
                              **{"app_jwt": lambda: "app-jwt", "poll_seconds": 60, **kwargs})
        stopping = asyncio.Event()

        async def run():
            await worker.start()
            try:
                await worker.run(stopping)
            finally:
                await worker.close()
        entry = (worker, stopping, asyncio.create_task(run()))
        self.running.append(entry)
        return entry

    async def wait_enriched(self, *ids: int, timeout: float = 10.0) -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            waiting = await self.db.fetchval(
                "select count(*) from raw_events where id = any($1::bigint[]) and enriched_at is null", list(ids))
            if waiting == 0:
                return time.monotonic() - started
            await asyncio.sleep(0.02)
        raise AssertionError(f"raw_events {ids} not enriched within {timeout}s")

    async def process(self, github: FakeGitHub, *ids: int, **kwargs) -> GitHubEnrich:
        worker, stopping, task = self.start(github, **kwargs)
        await self.wait_enriched(*ids)
        stopping.set()
        await asyncio.wait_for(task, 10)
        return worker

    async def records(self):
        return await self.db.fetch("select * from source_records order by kind, object_id")

    async def unenriched(self) -> list[int]:
        return [r["id"] for r in await self.db.fetch("select id from raw_events where enriched_at is null order by id")]

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
               await self.insert("pull_request:opened", pull_request_opened(), occurred_at=T0 + timedelta(hours=12))]
        await self.process(github, *ids)

        issue, pull = await self.records()
        self.assertEqual((issue["kind"], issue["object_id"], issue["version"]),
                         ("issue", "acme/widget#42", "2026-09-23T22:15:00Z"))
        # occurred_at is raw_events.occurred_at, which intake set from the payload (EMBER-63)
        self.assertEqual(issue["occurred_at"], T0)
        self.assertEqual(json.loads(issue["record"]), {"payload": issue_opened()})
        self.assertEqual((pull["kind"], pull["object_id"], pull["version"], pull["tenant"]),
                         ("pull_request", "acme/widget#43", "2026-09-24T10:00:00Z", "acme"))
        self.assertEqual(pull["occurred_at"], T0 + timedelta(hours=12))
        self.assertEqual(json.loads(pull["record"])["fetched"], {"files": FakeGitHub.FILES})
        self.assertEqual(github.paths()[-1], "/repos/acme/widget/pulls/43/files")

    async def test_claims_in_the_sources_order_not_arrival_order(self):
        later = await self.insert("issues:opened", issue_opened(), occurred_at=T0 + timedelta(minutes=5))
        earlier = await self.insert("issues:edited", issue_opened() | {"action": "edited"}, occurred_at=T0)
        await self.process(FakeGitHub(), later, earlier, concurrency=1, batch=1)
        order = [r["id"] for r in await self.db.fetch("select id from raw_events order by enriched_at")]
        self.assertEqual(order, [earlier, later])

    async def test_rows_without_a_tenant_wait(self):
        waiting = await self.insert("issues:opened", issue_opened(), tenant=None)
        done = await self.insert("issues:opened", issue_opened())
        await self.process(FakeGitHub(), done)
        self.assertEqual(await self.unenriched(), [waiting])

    async def test_full_push_uses_compare_and_small_push_does_not(self):
        small = FakeGitHub()
        await self.process(small, await self.insert("push", push(2)))
        self.assertEqual(small.calls, [])
        commits = [r for r in await self.records() if r["kind"] == "commit"]
        self.assertEqual(len(commits), 2)

        await self.db.execute("truncate source_records, episodes")
        full = FakeGitHub()
        await self.process(full, await self.insert("push", push(FULL_PUSH), occurred_at=datetime.fromtimestamp(1790326800, timezone.utc)))
        self.assertIn(f"/repos/acme/widget/compare/{'a' * 40}...{'b' * 40}", full.paths())
        rows = await self.records()
        self.assertEqual(sorted(r["version"] for r in rows if r["kind"] == "commit"),
                         [c["sha"] for c in FakeGitHub.COMPARE])
        (ref,) = [r for r in rows if r["kind"] == "ref"]
        self.assertEqual((ref["object_id"], ref["version"]), ("acme/widget:refs/heads/main", "b" * 40))
        self.assertEqual(ref["occurred_at"].timestamp(), 1790326800)

    async def test_installation_events_are_enriched_without_a_record_or_a_call(self):
        # No `repository` on these (github-webhooks.md, "Event types")
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
        self.start(FakeGitHub())
        await self.wait_enriched(before, timeout=5)
        after = await self.insert("pull_request:edited", pull_request_opened() | {"action": "edited"})
        self.assertLess(await self.wait_enriched(after, timeout=5), 1.0)

    async def test_stopping_finishes_the_row_in_flight(self):
        github = FakeGitHub(slow=1.0)
        row = await self.insert("pull_request:opened", pull_request_opened())
        _worker, stopping, task = self.start(github)
        self.assertTrue(await asyncio.to_thread(github.in_files.wait, 5))
        stopping.set()
        await asyncio.wait_for(task, 5)
        enriched_at = await self.db.fetchval("select enriched_at from raw_events where id = $1", row)
        self.assertIsNotNone(enriched_at)
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
        worker = await self.process(github, *range(1, 301), *pulls, issue)

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
        self.assertEqual(worker.status()["skipped_unmapped"], 300)

    async def test_retry_after_is_stored_and_the_next_call_waits_for_it(self):
        github = FakeGitHub(retry_after=2)
        await self.process(github, await self.insert("pull_request:opened", pull_request_opened()))
        limit = await self.db.fetchrow("select * from rate_limits where source = 'github' and tenant = 'acme' and bucket = '77'")
        throttled_at, retried_at = [at for at, url in github.calls if url.split("?")[0].endswith("/files")]
        next_allowed = limit["next_allowed_at"].timestamp()
        self.assertAlmostEqual(next_allowed - throttled_at, 2.0, delta=0.5)
        self.assertGreaterEqual(retried_at, next_allowed - 0.1)

    async def test_a_long_rate_limit_sets_the_row_aside_instead_of_holding_it(self):
        # Ten minutes is the queue's to wait out: the row is released, not slept on in its transaction
        github = FakeGitHub(retry_after=600)
        pull = await self.insert("pull_request:opened", pull_request_opened())
        worker, stopping, task = self.start(github)
        self.assertTrue(await asyncio.to_thread(github.in_files.wait, 5))
        for _ in range(100):
            if worker.status()["retrying"]:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(worker.status()["retrying"], 1)
        self.assertEqual(await self.unenriched(), [pull])
        # Released once the job commits, well inside the ten minutes: free for another replica to look at
        async def unlocked() -> bool:
            async with self.db.transaction():
                return await self.db.fetchval(
                    "select id from raw_events where id = $1 and enriched_at is null for update skip locked", pull) == pull
        for _ in range(100):
            if await unlocked():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(await unlocked())
        limit = await self.db.fetchval("select next_allowed_at from rate_limits where bucket = '77'")
        self.assertGreater(limit, datetime.now(timezone.utc) + timedelta(minutes=9))

    async def test_a_missing_object_is_set_aside_and_the_rest_still_go(self):
        github = FakeGitHub(files_status=404)
        pull = await self.insert("pull_request:opened", pull_request_opened())
        issue = await self.insert("issues:opened", issue_opened())
        with self.assertLogs("ember.work", "ERROR") as logs:
            await self.process(github, issue)
            await asyncio.sleep(0.2)
        self.assertEqual(await self.unenriched(), [pull])
        self.assertIn("permanent, retrying in 3600s: Permanent: GET /pulls/43/files failed (404)", "\n".join(logs.output))

    async def test_without_a_github_app_rows_that_need_a_call_wait_and_the_rest_go(self):
        github = FakeGitHub()
        pull = await self.insert("pull_request:opened", pull_request_opened())
        issue = await self.insert("issues:opened", issue_opened())
        with self.assertLogs("ember.work", "ERROR") as logs:
            await self.process(github, issue, app_jwt=missing_app("GITHUB_APP_ID is not set"))
            await asyncio.sleep(0.2)
        self.assertEqual(await self.unenriched(), [pull])
        self.assertEqual(github.calls, [])
        self.assertIn("GITHUB_APP_ID is not set: this event needs a GitHub API call", "\n".join(logs.output))


def pull(number: int, updated_at: str) -> dict:
    return {"id": 20000 + number, "number": number, "title": f"Pull {number}", "state": "open", "body": "",
            "head": {"ref": f"branch-{number}", "sha": f"{number:x}" * 40}, "base": {"ref": "main", "sha": "d" * 40},
            "review_comments": 1, "created_at": updated_at, "updated_at": updated_at}


class FakeRepo:
    """GitHub for backfill:github: installation 77 sees acme/widget, whose issues list (2 a page, by update) holds
    issue #42 (the fixture's, with a comment), pull request #43 (a conversation comment, a review comment, a
    submitted and a pending review) and issue #44. `hold` blocks the list's second page until `release`, and
    `throttle` answers the first call to each of its paths with that response."""

    def __init__(self, *, hold: bool = False, throttle: dict[str, api.Response] | None = None) -> None:
        self.issues = [
            FIXTURE["body"]["issue"] | {"comments": 1},
            {"number": 43, "title": "Pull 43", "updated_at": "2026-09-24T10:00:00Z", "comments": 1,
             "pull_request": {"url": "https://api.github.com/repos/acme/widget/pulls/43"}},
            {"number": 44, "title": "Later", "state": "open", "updated_at": "2026-10-02T08:00:00Z", "comments": 0},
        ]
        self.calls: list[tuple[float, str]] = []
        self.holding = threading.Event() if hold else None
        self.release = threading.Event()
        self.throttle = dict(throttle or {})

    def issue_queries(self) -> list[dict]:
        return [parse_qs(urlsplit(url).query) for _at, url in self.calls if urlsplit(url).path.endswith("/widget/issues")]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> api.Response:
        self.calls.append((time.time(), url))
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        if path in self.throttle:
            return self.throttle.pop(path)
        if path == "/app/installations/77/access_tokens":
            return ok({"token": "installation-token"})
        if path == "/installation/repositories":
            return ok({"total_count": 1, "repositories": [REPOSITORY]})
        if path == "/repos/acme/widget/issues":
            since = query.get("since", [""])[0]
            items = sorted((i for i in self.issues if i["updated_at"] >= since), key=lambda i: i["updated_at"])
            page = int(query.get("page", ["1"])[0])
            if page == 2 and self.holding is not None:
                self.holding.set()
                self.release.wait(10)
            response = ok(items[2 * page - 2:2 * page])
            if len(items) > 2 * page:
                response.headers["Link"] = f'<{url.split("&page=")[0]}&page={page + 1}>; rel="next"'
            return response
        if path == "/repos/acme/widget/pulls/43":
            return ok(pull(43, "2026-09-24T10:00:00Z"))
        if path == "/repos/acme/widget/pulls/43/reviews":
            return ok([{"id": 801, "state": "APPROVED", "body": "", "submitted_at": "2026-09-24T11:00:00Z"},
                       {"id": 802, "state": "PENDING", "body": "draft"}])
        if path == "/repos/acme/widget/pulls/43/comments":
            return ok([{"id": 901, "body": "nit", "path": "app/login.py", "updated_at": "2026-09-24T11:00:00Z"}])
        if path == "/repos/acme/widget/pulls/43/files":
            return ok(FakeGitHub.FILES)
        if path in ("/repos/acme/widget/issues/42/comments", "/repos/acme/widget/issues/43/comments"):
            number = int(path.split("/")[-2])
            return ok([{"id": 700 + number, "body": f"on #{number}", "updated_at": "2026-09-25T09:00:00Z"}])
        raise AssertionError(f"unexpected call {method} {url}")


# Every object FakeRepo holds, as the delivery_id the backfill gives it: "<object id>@<version>"
BACKFILLED = sorted([
    "acme/widget#42@2026-09-23T22:15:00Z", "acme/widget/issue-comment/742@2026-09-25T09:00:00Z",
    "acme/widget#43@2026-09-24T10:00:00Z", "acme/widget/review/801@2026-09-24T11:00:00Z",
    "acme/widget/review-comment/901@2026-09-24T11:00:00Z", "acme/widget/issue-comment/743@2026-09-25T09:00:00Z",
    "acme/widget#44@2026-10-02T08:00:00Z",
])


class BackfillTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, source_records, episodes, rate_limits, cursors, installations"
                              " restart identity cascade")
        # What Connect GitHub writes for installation 77
        await self.db.execute("insert into installations (source, account_id, tenant, label)"
                              " values ('github', '77', 'acme', 'acme')")

    async def asyncTearDown(self):
        await self.db.close()

    def component(self, github: FakeRepo, **kwargs) -> GitHubBackfill:
        return GitHubBackfill(Database(database_url()), Credentials(Database(database_url()), {}),
                              app_jwt=lambda: "app-jwt", exchange=github, **kwargs)

    async def backfill(self, github: FakeRepo, **kwargs) -> GitHubBackfill:
        component = self.component(github, **kwargs)
        await component.start()
        try:
            await component.run_once(asyncio.Event())
        finally:
            await component.close()
        return component

    async def delivery_ids(self) -> list[str]:
        return [r["delivery_id"] for r in await self.db.fetch(
            "select delivery_id from raw_events where origin = 'reconcile' order by delivery_id collate \"C\"")]

    async def killed(self, github: FakeRepo) -> None:
        """Start a pass and kill it while the issues list's second page is in flight."""
        component = self.component(github)
        await component.start()
        task = asyncio.create_task(component.run_once(asyncio.Event()))
        self.assertTrue(await asyncio.to_thread(github.holding.wait, 10))
        task.cancel()
        github.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await component.close()

    async def test_a_connected_installation_is_backfilled_and_enriched_like_webhooks(self):
        component = await self.backfill(FakeRepo())
        self.assertEqual(await self.delivery_ids(), BACKFILLED)
        raw = await self.db.fetchrow("select * from raw_events where delivery_id = 'acme/widget#44@2026-10-02T08:00:00Z'")
        self.assertEqual((raw["event_name"], raw["account_id"], raw["tenant"]), ("issues", "77", "acme"))
        self.assertEqual(raw["occurred_at"], datetime(2026, 10, 2, 8, tzinfo=timezone.utc))
        self.assertEqual(component.last["rows"], 7)
        self.assertEqual(await self.db.fetchval("select position from cursors where stream = 'repo:acme/widget:issues'"),
                         "2026-10-02T08:00:00Z")

        # A webhook copy of one of them is no new record, and the pull request gets its files
        webhook = await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope, occurred_at)"
            " values ('github', 'delivery-42', '77', 'acme', 'issues:opened', 'webhook', $1::jsonb, $2) returning id",
            json.dumps({"type": "github", "body": issue_opened()}), T0)
        worker = GitHubEnrich(Database(database_url()), exchange=FakeGitHub(), app_jwt=lambda: "app-jwt")
        await worker.start()
        stopping = asyncio.Event()
        task = asyncio.create_task(worker.run(stopping))
        try:
            while await self.db.fetchval("select count(*) from raw_events where enriched_at is null"):
                await asyncio.sleep(0.02)
        finally:
            stopping.set()
            await asyncio.wait_for(task, 10)
            await worker.close()
        rows = await self.db.fetch("select * from source_records order by object_id")
        self.assertEqual(sorted(f"{r['object_id']}@{r['version']}" for r in rows), BACKFILLED)
        self.assertEqual(sum(r["object_id"] == "acme/widget#42" for r in rows), 1)
        self.assertIsNotNone(webhook)
        (pr,) = [r for r in rows if r["kind"] == "pull_request"]
        self.assertEqual(json.loads(pr["record"])["fetched"], {"files": FakeGitHub.FILES})

    async def test_the_next_pass_reads_only_what_is_new_and_waits_its_turn(self):
        github = FakeRepo()
        await self.backfill(github)
        github.calls.clear()
        await self.backfill(github)  # read within the hour: not due
        self.assertEqual(github.calls, [])

        await self.db.execute("update cursors set updated_at = now() - interval '2 hours' where stream = 'installation:77'")
        github.issues.append({"number": 45, "title": "New", "state": "open", "updated_at": "2026-10-03T08:00:00Z",
                              "comments": 0})
        component = await self.backfill(github)
        self.assertEqual(github.issue_queries()[0]["since"], ["2026-10-02T07:59:59Z"])
        self.assertEqual(component.last["rows"], 1)  # #44 again is a no-op

    async def test_a_killed_pass_resumes_from_its_cursor_without_duplicates(self):
        await self.killed(FakeRepo(hold=True))
        self.assertEqual(await self.db.fetchval("select position from cursors where stream = 'repo:acme/widget:issues'"),
                         "2026-09-24T10:00:00Z")
        self.assertEqual(len(await self.delivery_ids()), 6)
        self.assertIsNone(await self.db.fetchval("select position from cursors where stream = 'installation:77'"))

        github = FakeRepo()
        component = await self.backfill(github)  # still due: the killed pass never finished
        self.assertEqual(github.issue_queries()[0]["since"], ["2026-09-24T09:59:59Z"])
        self.assertEqual(await self.delivery_ids(), BACKFILLED)
        self.assertEqual(component.last["rows"], 1)

    async def test_a_429_and_a_secondary_rate_limit_are_waited_out(self):
        secondary = api.Response(403, {"Retry-After": "1"}, b'{"message": "You have exceeded a secondary rate limit."}')
        github = FakeRepo(throttle={"/installation/repositories": api.Response(429, {"Retry-After": "1"}, b"{}"),
                                    "/repos/acme/widget/pulls/43/reviews": secondary})
        await self.backfill(github)
        self.assertEqual(await self.delivery_ids(), BACKFILLED)
        for path in ("/installation/repositories", "/repos/acme/widget/pulls/43/reviews"):
            throttled, retried = [at for at, url in github.calls if urlsplit(url).path == path]
            self.assertGreaterEqual(retried - throttled, 0.9, path)

    async def test_a_disconnected_installation_stops_and_is_not_read_again(self):
        github = FakeRepo(hold=True)
        component = self.component(github)
        await component.start()
        task = asyncio.create_task(component.run_once(asyncio.Event()))
        self.assertTrue(await asyncio.to_thread(github.holding.wait, 10))
        await self.db.execute("update installations set disconnected_at = now()")
        github.release.set()
        await asyncio.wait_for(task, 10)
        self.assertEqual(await component.due(), [])
        await component.close()
        self.assertEqual(len(await self.delivery_ids()), 6)  # #44, on the second page, isn't written
        self.assertIsNone(await self.db.fetchval("select position from cursors where stream = 'installation:77'"))

    async def test_reconcile_since_limits_a_first_pass(self):
        github = FakeRepo()
        await self.backfill(github, since=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(github.issue_queries()[0]["since"], ["2026-10-01T00:00:00Z"])
        self.assertEqual(await self.delivery_ids(), ["acme/widget#44@2026-10-02T08:00:00Z"])


class AppSettingsTest(unittest.TestCase):
    """How GITHUB_APP_* become the worker's JWT, or why the worker is disabled."""

    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        cls.public = key.public_key()

    def plan(self, **env: str):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "EMBER_"))}
        with mock.patch.dict(os.environ, {**clean, "DATABASE_URL": database_url(), **env}, clear=True):
            settings = Settings.load()
            return plan(Deps.build(settings), CONNECTORS)

    def worker(self, **env: str) -> GitHubEnrich:
        (worker,) = [c for c in self.plan(**env).components if c.name == "enrich:github"]
        return worker

    def test_the_key_from_a_file_or_the_environment_signs_the_apps_jwt(self):
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as handle:
            handle.write(self.pem)
            handle.flush()
            for env in ({"GITHUB_APP_PRIVATE_KEY_PATH": handle.name}, {"GITHUB_APP_PRIVATE_KEY": self.pem}):
                with self.subTest(next(iter(env))):
                    token = self.worker(GITHUB_APP_ID="5075660", **env).app_jwt()
                    claims = jwt.decode(token, self.public, algorithms=["RS256"])
                    self.assertEqual(claims["iss"], "5075660")
                    self.assertLessEqual(claims["exp"] - claims["iat"], 600)  # GitHub's limit

    def test_an_unusable_key_disables_the_worker_without_echoing_it(self):
        cases = [({"GITHUB_APP_PRIVATE_KEY_PATH": "/nonexistent/key.pem"},
                  "GITHUB_APP_PRIVATE_KEY_PATH can't be read: FileNotFoundError"),
                 ({"GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----"},
                  "GITHUB_APP_PRIVATE_KEY is not a PEM private key")]
        for env, reason in cases:
            with self.subTest(reason):
                built = self.plan(GITHUB_APP_ID="5075660", GITHUB_WEBHOOK_SECRET="s", **env)
                self.assertEqual(built.disabled["workers:github"], reason)
                self.assertNotIn("not-a-key", json.dumps(built.disabled))
                self.assertNotIn("enrich:github", [c.name for c in built.components])
                self.assertIn("intake", [c.name for c in built.components])  # webhooks still stored

    def test_without_an_app_the_worker_still_runs(self):
        self.assertEqual(self.worker().name, "enrich:github")
        with self.assertRaisesRegex(Exception, "GITHUB_APP_ID is not set"):
            self.worker().app_jwt()

    def test_concurrency_and_batch_come_from_the_environment(self):
        worker = self.worker(GITHUB_ENRICH_CONCURRENCY="2", GITHUB_ENRICH_BATCH="10")
        self.assertEqual((worker.concurrency, worker.batch), (2, 10))

    def test_the_backfill_reads_its_settings_and_needs_the_app(self):
        (backfill,) = [c for c in self.plan(GITHUB_BACKFILL_EVERY_HOURS="2", RECONCILE_SINCE="2026-10-01").components
                       if c.name == "backfill:github"]
        self.assertEqual((backfill.every, backfill.since),
                         (timedelta(hours=2), datetime(2026, 10, 1, tzinfo=timezone.utc)))
        with self.assertRaisesRegex(ConfigError, "^GITHUB_APP_ID is not set$"):
            asyncio.run(backfill.start())
        (backfill,) = [c for c in self.plan(RECONCILE_SINCE="").components if c.name == "backfill:github"]
        self.assertIsNone(backfill.since)  # as compose passes it unset


if __name__ == "__main__":
    unittest.main()
