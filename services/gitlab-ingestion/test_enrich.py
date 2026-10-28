"""enrich:gitlab against a real Postgres (EMBER-68). The GitLab API is stubbed at the exchange boundary.

Needs psql, dbmate, the packages in requirements.txt, and a Postgres server it may create and drop
a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/gitlab-ingestion/test_enrich.py

It builds the repo migrations in database ember_test_gitlab_enrich.
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
from models import RateLimit, RawEvent, SourceRecord  # noqa: E402
from pull import Response  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_gitlab_enrich"
FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "gitlab.json").read_text())
PROJECT = {**FIXTURE["body"]["project"], "default_branch": "main"}
USER = FIXTURE["body"]["user"]
ACCOUNT = "gitlab.com/acme"
UPDATED = "2026-10-01T09:30:00.000Z"
ZERO = "0" * 40


def database_url() -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def merge_request(action: str = "open", updated_at: str = UPDATED, **attrs) -> dict:
    body = FIXTURE["body"]
    attributes = {**body["object_attributes"], "action": action, "created_at": "2026-10-01T09:00:00.000Z",
                  "updated_at": updated_at, **attrs}
    return {**body, "project": PROJECT, "object_attributes": attributes, "labels": [], "changes": {}}


def issue(object_kind: str = "issue", iid: int = 3, updated_at: str = UPDATED, **attrs) -> dict:
    attributes = {"id": 300 + iid, "iid": iid, "title": "Intake drops retried deliveries", "state": "opened",
                  "action": "open", "confidential": False, "created_at": "2026-10-01T09:00:00.000Z",
                  "updated_at": updated_at, **attrs}
    return {"object_kind": object_kind, "event_type": object_kind, "user": USER, "project": PROJECT,
            "object_attributes": attributes, "labels": [], "changes": {}}


def note(**attrs) -> dict:
    attributes = {"id": 900, "note": "Keep the delivery id stable across retries", "noteable_type": "MergeRequest",
                  "noteable_id": 700, "internal": False, "action": "create", "created_at": "2026-10-01T10:00:00.000Z",
                  "updated_at": "2026-10-01T10:00:00.000Z", **attrs}
    return {"object_kind": "note", "event_type": "note", "user": USER, "project_id": 42, "project": PROJECT,
            "object_attributes": attributes, "merge_request": merge_request()["object_attributes"]}


def push(count: int, total: int | None = None, *, before: str = "a" * 40, after: str = "b" * 40,
         ref: str = "refs/heads/feature/intake") -> dict:
    commits = [{"id": f"{n:040x}", "message": f"commit {n}\n", "title": f"commit {n}",
                "timestamp": "2026-10-01T11:00:00+02:00", "author": {"name": "Pat Example", "email": "[REDACTED]"},
                "added": [], "modified": ["intake.py"], "removed": []} for n in range(count)]
    return {"object_kind": "push", "event_name": "push", "before": before, "after": after, "ref": ref,
            "checkout_sha": None if after == ZERO else after, "user_username": "pat", "project_id": 42,
            "project": PROJECT, "commits": commits, "total_commits_count": count if total is None else total}


def ok(data, **headers) -> Response:
    limits = {"RateLimit-Limit": "2000", "RateLimit-Remaining": "1999", "RateLimit-Reset": str(int(time.time()) + 60)}
    return Response(200, {**limits, **headers}, json.dumps(data).encode())


class FakeGitLab:
    """The exchange enrich calls. Records every call; `slow`, `throttle` and `low_quota` shape the responses."""

    DIFFS = [{"old_path": "intake.py", "new_path": "intake.py", "diff": "@@ -1 +1 @@\n-a\n+b\n",
              "new_file": False, "renamed_file": False, "deleted_file": False}]
    COMPARE = [{"id": f"{n:040x}", "short_id": f"{n:08x}", "title": f"compared {n}"} for n in range(100, 125)]

    def __init__(self, *, slow: float = 0.0, throttle: bool = False, low_quota: bool = False) -> None:
        self.slow = slow
        self.throttle = throttle
        self.low_quota = low_quota
        self.calls: list[tuple[float, str]] = []
        self.in_diffs = threading.Event()

    def paths(self) -> list[str]:
        return [urlsplit(url).path for _at, url in self.calls]

    def __call__(self, method: str, url: str, headers: dict) -> Response:
        assert headers["PRIVATE-TOKEN"] == "read-api-token"
        self.calls.append((time.time(), url))
        path = urlsplit(url).path
        if path.endswith("/diffs"):
            self.in_diffs.set()
            time.sleep(self.slow)
            if self.throttle:
                self.throttle = False
                return Response(429, {"Retry-After": "2"}, b'{"message": "429 Too Many Requests"}')
            return ok(self.DIFFS)
        if path.endswith("/repository/compare"):
            if self.low_quota:
                return ok({"commits": self.COMPARE, "diffs": []},
                          **{"RateLimit-Remaining": "3", "RateLimit-Reset": str(time.time() + 3)})
            return ok({"commits": self.COMPARE, "diffs": [{"diff": "huge"}]})
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

    async def insert(self, event_name: str, body: dict, delivery_id: str | None = None, origin: str = "webhook") -> int:
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
            " values ('gitlab', $1, $2, 'acme', $3, $4, $5::jsonb) returning id",
            delivery_id or f"delivery-{time.monotonic_ns()}", ACCOUNT, event_name, origin,
            json.dumps({"type": "gitlab", "body": body}),
        )

    def start(self, gitlab: FakeGitLab, token: str = "read-api-token") -> tuple[enrich.Worker, asyncio.Task]:
        worker = enrich.Worker(database_url(), api_token=token, exchange=gitlab)
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

    async def process(self, gitlab: FakeGitLab, *ids: int) -> None:
        worker, task = self.start(gitlab)
        await self.wait_enriched(*ids)
        worker.stop()
        await task

    async def records(self, kind: str | None = None):
        rows = await self.db.fetch("select * from source_records order by kind, object_id, version")
        return [r for r in rows if kind is None or r["kind"] == kind]

    async def received_at(self, raw_id: int):
        return await self.db.fetchval("select received_at from raw_events where id = $1", raw_id)

    async def test_models_match_the_migrated_schema(self):
        for model in (RawEvent, SourceRecord, RateLimit):
            table = model.__table__
            rows = await self.db.fetch(
                "select column_name, is_nullable = 'YES' as nullable from information_schema.columns"
                " where table_schema = 'public' and table_name = $1", table.name)
            self.assertEqual({(c.name, c.nullable) for c in table.columns},
                             {(r["column_name"], r["nullable"]) for r in rows}, table.name)

    async def test_merge_request_issue_work_item_and_note_become_source_records(self):
        gitlab = FakeGitLab()
        ids = [await self.insert("merge_request:open", merge_request()),
               await self.insert("issue:open", issue()),
               await self.insert("work_item:open", issue("work_item", iid=4)),
               await self.insert("note:create", note())]
        await self.process(gitlab, *ids)

        first_issue, work_item, pull, comment = await self.records()
        self.assertEqual((first_issue["kind"], first_issue["object_id"], first_issue["version"], first_issue["tenant"]),
                         ("issue", "42#3", UPDATED, "acme"))
        self.assertEqual(first_issue["occurred_at"].isoformat(), "2026-10-01T09:30:00+00:00")
        self.assertEqual(json.loads(first_issue["record"]), {"payload": issue()})
        self.assertEqual((work_item["kind"], work_item["object_id"]), ("issue", "42#4"))
        self.assertEqual((pull["kind"], pull["object_id"], pull["version"]), ("merge_request", "42!7", UPDATED))
        self.assertEqual(json.loads(pull["record"])["fetched"], {"diffs": FakeGitLab.DIFFS})
        self.assertEqual((comment["kind"], comment["object_id"], comment["version"]),
                         ("note", "42/note/900", "2026-10-01T10:00:00.000Z"))
        self.assertEqual(gitlab.paths(), ["/api/v4/projects/42/merge_requests/7/diffs"])

    async def test_only_open_reopen_and_new_commits_fetch_diffs(self):
        gitlab = FakeGitLab()
        ids = [await self.insert("merge_request:update", merge_request("update", "2026-10-01T09:40:00.000Z")),
               await self.insert("merge_request:update",
                                 merge_request("update", "2026-10-01T09:50:00.000Z", oldrev="c" * 40)),
               await self.insert("merge_request:close", merge_request("close", "2026-10-01T10:00:00.000Z")),
               await self.insert("merge_request:reopen", merge_request("reopen", "2026-10-01T10:10:00.000Z"))]
        await self.process(gitlab, *ids)
        self.assertEqual(len(gitlab.calls), 2)
        fetched = {r["version"]: "fetched" in json.loads(r["record"]) for r in await self.records()}
        self.assertEqual(fetched, {"2026-10-01T09:40:00.000Z": False, "2026-10-01T09:50:00.000Z": True,
                                   "2026-10-01T10:00:00.000Z": False, "2026-10-01T10:10:00.000Z": True})

    async def test_push_over_20_commits_uses_compare_and_a_short_one_does_not(self):
        short = FakeGitLab()
        short_id = await self.insert("push", push(2))
        await self.process(short, short_id)
        self.assertEqual(short.calls, [])
        self.assertEqual(len(await self.records("commit")), 2)
        (ref,) = await self.records("ref")
        self.assertEqual((ref["object_id"], ref["version"]), ("42:refs/heads/feature/intake", "b" * 40))
        self.assertEqual(ref["occurred_at"], await self.received_at(short_id))
        self.assertNotIn("commits", json.loads(ref["record"])["payload"])

        await self.db.execute("truncate source_records, episodes")
        long = FakeGitLab()
        await self.process(long, await self.insert("push", push(20, total=25)))
        (_at, url), = long.calls
        self.assertEqual(urlsplit(url).path, "/api/v4/projects/42/repository/compare")
        self.assertEqual(parse_qs(urlsplit(url).query), {"from": ["a" * 40], "to": ["b" * 40]})
        commits = await self.records("commit")
        self.assertEqual(sorted(r["version"] for r in commits), [c["id"] for c in FakeGitLab.COMPARE])
        self.assertEqual(commits[0]["object_id"], f"42@{FakeGitLab.COMPARE[0]['id']}")

    async def test_new_branch_compares_from_the_default_branch_and_a_delete_is_a_tombstone(self):
        gitlab = FakeGitLab()
        new_branch = await self.insert("push", push(20, total=25, before=ZERO))
        first_push = await self.insert("push", push(20, total=25, before=ZERO, after="d" * 40, ref="refs/heads/main"))
        deleted = await self.insert("push", push(0, before="b" * 40, after=ZERO))
        await self.process(gitlab, new_branch, first_push, deleted)

        (_at, url), = gitlab.calls
        self.assertEqual(parse_qs(urlsplit(url).query), {"from": ["main"], "to": ["b" * 40]})
        main = [r for r in await self.records("commit") if r["raw_event_id"] == first_push]
        self.assertEqual(len(main), 20, "the first push to an empty project keeps the payload's commits")
        tombstone = [r for r in await self.records("ref") if r["raw_event_id"] == deleted][0]
        self.assertEqual(tombstone["object_id"], "42:refs/heads/feature/intake")
        self.assertEqual(tombstone["version"], f"deleted:{enrich.stamp(await self.received_at(deleted))}")
        self.assertIs(json.loads(tombstone["record"])["tombstone"], True)

    async def test_an_approval_is_its_own_version_ordered_on_arrival(self):
        opened = await self.insert("merge_request:open", merge_request())
        approved = await self.insert("merge_request:approval", merge_request("approval"))
        await self.process(FakeGitLab(), opened, approved)
        first, second = await self.records()
        self.assertEqual(first["version"], UPDATED)
        received = await self.received_at(approved)
        self.assertEqual(second["version"], f"approval:{enrich.stamp(received)}")
        self.assertEqual(second["occurred_at"], received)

    async def test_webhook_and_rest_copies_of_one_version_fold_together(self):
        gitlab = FakeGitLab()
        rest_issue = {"id": 303, "iid": 3, "project_id": 42, "issue_type": "issue", "title": "Intake drops retried deliveries",
                      "state": "opened", "confidential": False, "updated_at": UPDATED}
        rest_mr = {"id": 700, "iid": 7, "project_id": 42, "source_branch": "feature/intake", "target_branch": "develop",
                   "updated_at": UPDATED}
        rest_note = {"id": 901, "body": "Reproduced on main", "noteable_type": "Issue", "noteable_iid": 3,
                     "project_id": 42, "internal": False, "confidential": False, "updated_at": "2026-10-02T08:00:00.123Z"}
        rest_project = {"id": 42, "path_with_namespace": "acme/widget", "last_activity_at": "2026-10-02T08:00:00.123Z"}
        ids = [await self.insert("issue:update", issue(updated_at="2026-10-01 09:30:00 UTC")),  # before GitLab 19.0
               await self.insert("issue", rest_issue, "42#3@" + UPDATED, origin="backfill"),
               await self.insert("merge_request", rest_mr, "42!7@" + UPDATED, origin="backfill"),
               await self.insert("note", rest_note, "42/note/901@2026-10-02T08:00:00.123Z", origin="backfill"),
               await self.insert("project", rest_project, "42@2026-10-02T08:00:00.123Z", origin="backfill")]
        await self.process(gitlab, *ids)

        self.assertEqual(gitlab.calls, [], "REST objects fetch nothing")
        self.assertEqual([(r["kind"], r["object_id"], r["version"]) for r in await self.records()], [
            ("issue", "42#3", UPDATED),
            ("merge_request", "42!7", UPDATED),
            ("note", "42/note/901", "2026-10-02T08:00:00.123Z"),
            ("project", "42", "2026-10-02T08:00:00.123Z"),
        ])

    async def test_confidential_internal_and_unsubscribed_events_are_marked_without_records(self):
        tag_push = {**push(0), "object_kind": "tag_push", "ref": "refs/tags/v1.0.0"}
        ids = [await self.insert("issue:open", issue(confidential=True)),
               await self.insert("note:create", note(internal=True)),
               await self.insert("note:create", {**note(id=902), "event_type": "confidential_note"}),
               await self.insert("tag_push", tag_push)]
        gitlab = FakeGitLab()
        await self.process(gitlab, *ids)
        self.assertEqual(await self.records(), [])
        self.assertEqual(gitlab.calls, [])

    async def test_reprocessing_and_duplicate_versions_leave_one_record(self):
        first = await self.insert("issue:open", issue())
        await self.process(FakeGitLab(), first)
        await self.db.execute("update raw_events set enriched_at = null")
        second = await self.insert("issue:open", issue(), delivery_id="resent-with-a-new-id")
        await self.process(FakeGitLab(), first, second)
        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), 1)

    async def test_catches_up_at_start_and_wakes_on_insert_within_a_second(self):
        before = await self.insert("issue:open", issue())
        worker, task = self.start(FakeGitLab())
        try:
            await self.wait_enriched(before, timeout=5)
            after = await self.insert("merge_request:update", merge_request("update"))
            self.assertLess(await self.wait_enriched(after, timeout=5), 1.0)
        finally:
            worker.stop()
            await task

    async def test_sigint_finishes_the_row_in_flight_then_exits(self):
        gitlab = FakeGitLab(slow=1.0)
        row = await self.insert("merge_request:open", merge_request())
        _worker, task = self.start(gitlab)
        self.assertTrue(await asyncio.to_thread(gitlab.in_diffs.wait, 5))
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(task, 5)
        enriched_at = await self.db.fetchval("select enriched_at from raw_events where id = $1", row)
        diffs_called_at = [at for at, url in gitlab.calls if "/diffs" in url][0]
        self.assertGreaterEqual(enriched_at.timestamp(), diffs_called_at + gitlab.slow - 0.1, "enriched_at predates the fetch")
        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), 1)

    async def test_retry_after_is_stored_and_the_next_call_waits_for_it(self):
        gitlab = FakeGitLab(throttle=True)
        await self.process(gitlab, await self.insert("merge_request:open", merge_request()))
        limit = await self.db.fetchrow(
            "select * from rate_limits where source = 'gitlab' and tenant = 'acme' and bucket = $1", ACCOUNT)
        throttled_at, retried_at = [at for at, url in gitlab.calls if urlsplit(url).path.endswith("/diffs")]
        next_allowed = limit["next_allowed_at"].timestamp()
        self.assertAlmostEqual(next_allowed - throttled_at, 2.0, delta=0.5)
        self.assertGreaterEqual(retried_at, next_allowed - 0.1)
        self.assertEqual(limit["remaining"], 1999)

    async def test_a_low_quota_holds_the_bucket_until_ratelimit_reset(self):
        gitlab = FakeGitLab(low_quota=True)
        await self.process(gitlab, await self.insert("push", push(20, total=25)))
        limit = await self.db.fetchrow("select * from rate_limits where source = 'gitlab' and bucket = $1", ACCOUNT)
        (called_at, _url), = gitlab.calls
        self.assertAlmostEqual(limit["next_allowed_at"].timestamp() - called_at, 3.0, delta=0.5)
        self.assertEqual(limit["remaining"], 3)

    async def test_a_fetch_without_a_token_fails_that_row_only(self):
        needs_fetch = await self.insert("merge_request:open", merge_request())
        no_fetch = await self.insert("issue:open", issue())
        worker, task = self.start(FakeGitLab(), token="")
        try:
            await self.wait_enriched(no_fetch)
            await asyncio.sleep(0.3)
            self.assertIsNone(await self.db.fetchval("select enriched_at from raw_events where id = $1", needs_fetch))
        finally:
            worker.stop()
            await task
        self.assertEqual([r["kind"] for r in await self.records()], ["issue"])


if __name__ == "__main__":
    unittest.main()
