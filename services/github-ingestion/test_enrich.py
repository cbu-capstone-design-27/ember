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
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import enrich  # noqa: E402
from models import Cursor, Installation, RateLimit, RawEvent, SourceRecord  # noqa: E402
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


def pull(number: int, updated_at: str) -> dict:
    return {"id": 20000 + number, "number": number, "title": f"Pull {number}", "state": "open", "body": "",
            "head": {"ref": f"branch-{number}", "sha": f"{number:x}" * 40}, "base": {"ref": "main", "sha": "d" * 40},
            "review_comments": 1, "created_at": updated_at, "updated_at": updated_at}


class FakeRepo:
    """GitHub for reconcile: installation 77 sees acme/widget, whose issues list (2 per page, by update)
    holds issue #42 (the fixture's, with a comment), pull request #43 (a conversation comment, a review
    comment, a submitted and a pending review) and issue #44. `hold` blocks the issues list's second
    page until released, and `throttle` answers the first call to each of those paths with that response."""

    ISSUES = [
        FIXTURE["body"]["issue"] | {"comments": 1},
        {"number": 43, "title": "Pull 43", "updated_at": "2026-09-24T10:00:00Z", "comments": 1,
         "pull_request": {"url": "https://api.github.com/repos/acme/widget/pulls/43"}},
        {"number": 44, "title": "Later", "state": "open", "updated_at": "2026-10-02T08:00:00Z", "comments": 0},
    ]

    def __init__(self, *, hold: bool = False, throttle: dict[str, Response] | None = None) -> None:
        self.calls: list[tuple[float, str]] = []
        self.holding = threading.Event() if hold else None
        self.release = threading.Event()
        self.throttle = dict(throttle or {})

    def paths(self) -> list[str]:
        return [urlsplit(url).path for _at, url in self.calls]

    def issue_queries(self) -> list[dict]:
        return [parse_qs(urlsplit(url).query) for _at, url in self.calls if urlsplit(url).path.endswith("/widget/issues")]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
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
            items = sorted((i for i in self.ISSUES if i["updated_at"] >= since), key=lambda i: i["updated_at"])
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


# Every object FakeRepo holds, as the delivery_id reconcile gives it: "<object id>@<version>"
RECONCILED = sorted([
    "acme/widget#42@2026-09-23T22:15:00Z", "acme/widget/issue-comment/742@2026-09-25T09:00:00Z",
    "acme/widget#43@2026-09-24T10:00:00Z", "acme/widget/review/801@2026-09-24T11:00:00Z",
    "acme/widget/review-comment/901@2026-09-24T11:00:00Z", "acme/widget/issue-comment/743@2026-09-25T09:00:00Z",
    "acme/widget#44@2026-10-02T08:00:00Z",
])


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
        await self.db.execute(
            "truncate raw_events, source_records, episodes, rate_limits, cursors, installations restart identity cascade")

    async def asyncTearDown(self):
        await self.db.close()

    async def insert(self, event_name: str, body: dict, delivery_id: str | None = None) -> int:
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
            " values ('github', $1, '77', 'acme', $2, 'webhook', $3::jsonb) returning id",
            delivery_id or f"delivery-{time.monotonic_ns()}", event_name, json.dumps({"type": "github", "body": body}),
        )

    def start(self, github, **kwargs) -> tuple[enrich.Worker, asyncio.Task]:
        worker = enrich.Worker(database_url(), exchange=github, app_jwt=lambda: "app-jwt", **kwargs)
        return worker, asyncio.create_task(worker.run())

    async def connect(self) -> None:
        """What Connect GitHub writes for installation 77."""
        await self.db.execute("insert into installations (source, account_id, tenant, label) values ('github', '77', 'acme', 'acme')")

    async def wait_for(self, sql: str, timeout: float = 10.0) -> None:
        started = time.monotonic()
        while not await self.db.fetchval(sql):
            if time.monotonic() - started > timeout:
                raise AssertionError(f"timed out waiting for: {sql}")
            await asyncio.sleep(0.02)

    async def reconcile(self, github, **kwargs) -> None:
        """Run the worker until installation 77's reconcile is done and every row it wrote is enriched."""
        worker, task = self.start(github, **kwargs)
        try:
            await self.wait_for("select exists (select 1 from cursors where stream = 'reconcile:77' and position = 'done')"
                                " and not exists (select 1 from raw_events where enriched_at is null)")
        finally:
            worker.stop()
            await asyncio.wait_for(task, 10)

    async def delivery_ids(self) -> list[str]:
        return [r["delivery_id"] for r in await self.db.fetch(
            "select delivery_id from raw_events where origin = 'reconcile' order by delivery_id collate \"C\"")]

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
        for model in (RawEvent, SourceRecord, RateLimit, Cursor, Installation):
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


    async def test_connect_backfills_issues_pulls_and_comments_through_enrich(self):
        await self.connect()
        github = FakeRepo()
        await self.reconcile(github)

        self.assertEqual(await self.delivery_ids(), RECONCILED)
        raw = await self.db.fetchrow("select * from raw_events where delivery_id = 'acme/widget#44@2026-10-02T08:00:00Z'")
        self.assertEqual((raw["event_name"], raw["account_id"], raw["tenant"]), ("issues", "77", "acme"))
        self.assertEqual(raw["occurred_at"].isoformat(), "2026-10-02T08:00:00+00:00")
        rows = await self.records()
        self.assertEqual(sorted(f"{r['object_id']}@{r['version']}" for r in rows), RECONCILED)
        self.assertEqual(sorted(r["kind"] for r in rows), ["issue", "issue", "issue_comment", "issue_comment",
                                                           "pull_request", "pull_request_review", "pull_request_review_comment"])
        (pr,) = [r for r in rows if r["kind"] == "pull_request"]
        self.assertEqual(json.loads(pr["record"])["fetched"], {"files": FakeGitHub.FILES})
        self.assertEqual(await self.db.fetch("select stream, position from cursors order by stream"),
                         [("reconcile:77", "done"), ("reconcile:acme/widget:issues", "done")])

        # Done means done: a restart lists nothing
        github = FakeRepo()
        worker, task = self.start(github)
        await asyncio.sleep(0.5)
        worker.stop()
        await task
        self.assertEqual(github.calls, [])

    async def test_a_killed_reconcile_resumes_from_its_cursor_without_duplicates(self):
        await self.connect()
        github = FakeRepo(hold=True)
        worker, task = self.start(github)
        self.assertTrue(await asyncio.to_thread(github.holding.wait, 10))
        task.cancel()   # killed while the second page is in flight
        worker.reconciling.cancel()
        github.release.set()
        await asyncio.gather(task, worker.reconciling, return_exceptions=True)
        self.assertEqual(await self.db.fetchval(
            "select position from cursors where stream = 'reconcile:acme/widget:issues'"), "2026-09-24T10:00:00Z")
        self.assertEqual(len(await self.delivery_ids()), 6)

        github = FakeRepo()
        await self.reconcile(github)
        self.assertEqual(github.issue_queries()[0]["since"], ["2026-09-24T09:59:59Z"])
        self.assertEqual(await self.delivery_ids(), RECONCILED)
        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), len(RECONCILED))

    async def test_a_webhook_and_a_reconcile_copy_of_one_version_make_one_record(self):
        await self.connect()
        await self.insert("issues:opened", issue_opened())
        await self.reconcile(FakeRepo())
        self.assertEqual(await self.db.fetchval("select count(*) from raw_events where delivery_id like 'acme/widget#42@%'"
                                                " or origin = 'webhook'"), 2)
        self.assertEqual(await self.db.fetchval(
            "select count(*) from source_records where object_id = 'acme/widget#42'"), 1)

    async def test_a_429_and_a_secondary_rate_limit_wait_and_continue(self):
        await self.connect()
        secondary = Response(403, {"Retry-After": "1"}, b'{"message": "You have exceeded a secondary rate limit."}')
        github = FakeRepo(throttle={"/installation/repositories": Response(429, {"Retry-After": "1"}, b"{}"),
                                    "/repos/acme/widget/pulls/43/reviews": secondary})
        await self.reconcile(github)
        self.assertEqual(await self.delivery_ids(), RECONCILED)
        for path in ("/installation/repositories", "/repos/acme/widget/pulls/43/reviews"):
            throttled, retried = [at for at, url in github.calls if urlsplit(url).path == path]
            self.assertGreaterEqual(retried - throttled, 0.9, path)

    async def test_a_disconnected_installation_is_not_reconciled(self):
        await self.connect()
        github = FakeRepo(hold=True)
        worker, task = self.start(github, poll_seconds=0.2)
        self.assertTrue(await asyncio.to_thread(github.holding.wait, 10))
        await self.db.execute("update installations set disconnected_at = now()")
        github.release.set()
        await asyncio.sleep(1.0)   # the held page's rows, then several polls
        worker.stop()
        await task
        self.assertEqual(len(await self.delivery_ids()), 6)   # the first page only
        self.assertEqual(await self.db.fetchval("select count(*) from cursors where position = 'done'"), 0)
        self.assertEqual(sum(path.endswith("/widget/issues") for path in github.paths()), 2)

    async def test_reconcile_since_limits_the_pull(self):
        await self.connect()
        github = FakeRepo()
        await self.reconcile(github, reconcile_since=enrich.reconcile_since("2026-10-01"))
        self.assertEqual(github.issue_queries()[0]["since"], ["2026-10-01T00:00:00Z"])
        self.assertEqual(await self.delivery_ids(), ["acme/widget#44@2026-10-02T08:00:00Z"])


if __name__ == "__main__":
    unittest.main()
