"""enrich:gitlab (EMBER-68, rebuilt on the backend in EMBER-160) against a real Postgres. GitLab is stubbed
at the exchange.

Needs psql, dbmate, this package installed (`pip install services/backend`), and a Postgres server it
may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \\
        python3 services/backend/tests/test_enrich_gitlab.py

It builds the repo migrations in database ember_test_enrich_gitlab. The record cases carried over from
services/gitlab-ingestion/test_enrich.py keep their names.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

from ember.connectors.gitlab.enrich import GitLabEnrich, stamp  # noqa: E402
from ember.core.credentials import Credentials  # noqa: E402
from ember.core.db import Database  # noqa: E402
from ember.core.errors import ConfigError  # noqa: E402
from ember.core.http import Response  # noqa: E402
from source_credentials import KeyRing  # noqa: E402

MIGRATIONS = REPO / "services" / "backend" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_enrich_gitlab"
FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "gitlab.json").read_text())
PROJECT = {**FIXTURE["body"]["project"], "default_branch": "main"}
USER = FIXTURE["body"]["user"]
ACCOUNT = "gitlab.com/acme"
TENANT = "acme"
TOKEN = "stored-token"
UPDATED = "2026-10-01T09:30:00.000Z"
ZERO = "0" * 40
KEY = bytes(range(32))
RING = KeyRing("api", [("test", KEY)])
ENV = {"SOURCE_CREDENTIALS_API_KEYS": "test:" + base64.b64encode(KEY).decode()}


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
                "timestamp": "2026-10-01T11:00:00+02:00", "author": {"name": "Pat Example"},
                "added": [], "modified": ["intake.py"], "removed": []} for n in range(count)]
    return {"object_kind": "push", "event_name": "push", "before": before, "after": after, "ref": ref,
            "checkout_sha": None if after == ZERO else after, "user_username": "pat", "project_id": 42,
            "project": PROJECT, "commits": commits, "total_commits_count": count if total is None else total}


def ok(data, **headers) -> Response:
    limits = {"RateLimit-Limit": "2000", "RateLimit-Remaining": "1999", "RateLimit-Reset": str(int(time.time()) + 60)}
    return Response(200, {**limits, **headers}, json.dumps(data).encode())


class FakeGitLab:
    """The exchange enrich calls. Records every call; the options shape the responses."""

    DIFFS = [{"old_path": "intake.py", "new_path": "intake.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"}]
    COMPARE = [{"id": f"{n:040x}", "short_id": f"{n:08x}", "title": f"compared {n}"} for n in range(100, 125)]

    def __init__(self, *, throttle: bool = False, refuse: bool = False, pages: int = 1) -> None:
        self.throttle = throttle
        self.refuse = refuse
        self.pages = pages
        self.calls: list[tuple[float, str]] = []

    def paths(self) -> list[str]:
        return [urlsplit(url).path for _at, url in self.calls]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        self.calls.append((time.time(), url))
        if self.refuse or headers["PRIVATE-TOKEN"] != TOKEN:
            return Response(401, {}, b'{"message": "401 Unauthorized"}')
        parts = urlsplit(url)
        if parts.path.endswith("/diffs"):
            if self.throttle:
                self.throttle = False
                return Response(429, {"Retry-After": "2"}, b'{"message": "429 Too Many Requests"}')
            page = int(parse_qs(parts.query).get("page", ["1"])[0])
            headers = {}
            if page < self.pages:
                headers["Link"] = f'<{parts.scheme}://{parts.netloc}{parts.path}?per_page=100&page={page + 1}>; rel="next"'
            return ok(self.DIFFS, **headers)
        if parts.path.endswith("/repository/compare"):
            return ok({"commits": self.COMPARE, "diffs": [{"diff": "huge"}]})
        raise AssertionError(f"unexpected call {method} {url}")


class EnrichTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, source_records, rate_limits, installations, source_credentials"
                              " restart identity cascade")
        self.credentials = Credentials(Database(database_url()), ENV)
        self.running: list[tuple[asyncio.Event, asyncio.Task]] = []

    async def asyncTearDown(self):
        await self.stop()
        await self.credentials.close()
        await self.db.close()

    async def stop(self) -> None:
        for stopping, task in self.running:
            stopping.set()
            await asyncio.wait_for(task, 10)
        self.running = []

    async def connect(self, account: str = ACCOUNT, tenant: str = TENANT, *, base_url: str = "https://gitlab.com",
                      disconnected: bool = False) -> None:
        """What Connect GitLab writes (apps/web/src/lib/gitlab-connect.ts): the group, and its sealed api row."""
        await self.db.execute("insert into installations (source, account_id, tenant, disconnected_at)"
                              " values ('gitlab', $1, $2, case when $3 then now() end)", account, tenant, disconnected)
        sealed = RING.seal("gitlab", account, {"token": TOKEN, "base_url": base_url, "token_kind": "group"})
        await self.db.execute("insert into source_credentials (source, account_id, kind, key_id, ciphertext)"
                              " values ('gitlab', $1, 'api', $2, $3)", account, sealed.key_id, sealed.ciphertext)

    async def insert(self, event_name: str, body: dict, delivery_id: str | None = None, origin: str = "webhook",
                     account: str = ACCOUNT) -> int:
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
            " values ('gitlab', $1, $2, $3, $4, $5, $6::jsonb) returning id",
            delivery_id or f"delivery-{time.monotonic_ns()}", account, TENANT, event_name, origin,
            json.dumps({"type": "gitlab", "body": body}))

    def start(self, gitlab: FakeGitLab) -> GitLabEnrich:
        worker = GitLabEnrich(Database(database_url()), self.credentials, exchange=gitlab, poll_seconds=60)
        stopping = asyncio.Event()

        async def go():
            await worker.start()
            try:
                await worker.run(stopping)
            finally:
                await worker.close()
        self.running.append((stopping, asyncio.create_task(go())))
        return worker

    async def wait_enriched(self, *ids: int, timeout: float = 10.0) -> None:
        started = time.monotonic()
        while await self.db.fetchval("select count(*) from raw_events where id = any($1::bigint[])"
                                     " and enriched_at is null", list(ids)):
            if time.monotonic() - started > timeout:
                raise AssertionError(f"raw_events {ids} not enriched within {timeout}s")
            await asyncio.sleep(0.02)

    async def process(self, gitlab: FakeGitLab, *ids: int) -> None:
        self.start(gitlab)
        await self.wait_enriched(*ids)
        await self.stop()

    async def set_aside(self, gitlab: FakeGitLab, needs_fetch: int, no_fetch: int) -> GitLabEnrich:
        """Run until `no_fetch` is enriched; `needs_fetch` must still be waiting, set aside to retry."""
        worker = self.start(gitlab)
        await self.wait_enriched(no_fetch)
        await asyncio.sleep(0.3)
        await self.stop()
        self.assertIsNone(await self.db.fetchval("select enriched_at from raw_events where id = $1", needs_fetch))
        return worker

    async def records(self, kind: str | None = None):
        rows = await self.db.fetch("select * from source_records order by kind, object_id, version")
        return [r for r in rows if kind is None or r["kind"] == kind]

    async def received_at(self, raw_id: int):
        return await self.db.fetchval("select received_at from raw_events where id = $1", raw_id)

    async def test_merge_request_issue_work_item_and_note_become_source_records(self):
        await self.connect()
        gitlab = FakeGitLab()
        ids = [await self.insert("merge_request:open", merge_request()),
               await self.insert("issue:open", issue()),
               await self.insert("work_item:open", issue("work_item", iid=4)),
               await self.insert("note:create", note())]
        await self.process(gitlab, *ids)

        first_issue, work_item, pull, comment = await self.records()
        self.assertEqual((first_issue["kind"], first_issue["object_id"], first_issue["version"], first_issue["tenant"]),
                         ("issue", "42#3", UPDATED, TENANT))
        self.assertEqual(json.loads(first_issue["record"]), {"payload": issue()})
        self.assertEqual((work_item["kind"], work_item["object_id"]), ("issue", "42#4"))
        self.assertEqual((pull["kind"], pull["object_id"], pull["version"]), ("merge_request", "42!7", UPDATED))
        self.assertEqual(json.loads(pull["record"])["fetched"], {"diffs": FakeGitLab.DIFFS})
        self.assertEqual((comment["kind"], comment["object_id"], comment["version"]),
                         ("note", "42/note/900", "2026-10-01T10:00:00.000Z"))
        self.assertEqual([url.split("?")[0] for _at, url in gitlab.calls],
                         ["https://gitlab.com/api/v4/projects/42/merge_requests/7/diffs"])

    async def test_occurred_at_is_the_rows_own(self):
        raw = await self.insert("issue:open", issue())
        await self.db.execute("update raw_events set occurred_at = '2026-10-01T09:30:00Z' where id = $1", raw)
        await self.process(FakeGitLab(), raw)
        (record,) = await self.records()
        self.assertEqual(record["occurred_at"].isoformat(), "2026-10-01T09:30:00+00:00")

    async def test_only_open_reopen_and_new_commits_fetch_diffs(self):
        await self.connect()
        gitlab = FakeGitLab(pages=2)
        ids = [await self.insert("merge_request:update", merge_request("update", "2026-10-01T09:40:00.000Z")),
               await self.insert("merge_request:update",
                                 merge_request("update", "2026-10-01T09:50:00.000Z", oldrev="c" * 40)),
               await self.insert("merge_request:close", merge_request("close", "2026-10-01T10:00:00.000Z")),
               await self.insert("merge_request:reopen", merge_request("reopen", "2026-10-01T10:10:00.000Z"))]
        await self.process(gitlab, *ids)
        self.assertEqual(len(gitlab.calls), 4, "two fetches, two pages each")
        fetched = {r["version"]: len(json.loads(r["record"]).get("fetched", {}).get("diffs", []))
                   for r in await self.records()}
        self.assertEqual(fetched, {"2026-10-01T09:40:00.000Z": 0, "2026-10-01T09:50:00.000Z": 2,
                                   "2026-10-01T10:00:00.000Z": 0, "2026-10-01T10:10:00.000Z": 2})

    async def test_push_over_20_commits_uses_compare_and_a_short_one_does_not(self):
        await self.connect()
        short = FakeGitLab()
        short_id = await self.insert("push", push(2))
        await self.process(short, short_id)
        self.assertEqual(short.calls, [])
        self.assertEqual(len(await self.records("commit")), 2)
        (ref,) = await self.records("ref")
        self.assertEqual((ref["object_id"], ref["version"]), ("42:refs/heads/feature/intake", "b" * 40))
        self.assertNotIn("commits", json.loads(ref["record"])["payload"])

        await self.db.execute("truncate source_records cascade")
        long = FakeGitLab()
        await self.process(long, await self.insert("push", push(20, total=25)))
        (_at, url), = long.calls
        self.assertEqual(urlsplit(url).path, "/api/v4/projects/42/repository/compare")
        self.assertEqual(parse_qs(urlsplit(url).query), {"from": ["a" * 40], "to": ["b" * 40]})
        commits = await self.records("commit")
        self.assertEqual(sorted(r["version"] for r in commits), [c["id"] for c in FakeGitLab.COMPARE])
        self.assertEqual(commits[0]["object_id"], f"42@{FakeGitLab.COMPARE[0]['id']}")

    async def test_new_branch_compares_from_the_default_branch_and_a_delete_is_a_tombstone(self):
        await self.connect()
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
        self.assertEqual(tombstone["version"], f"deleted:{stamp(await self.received_at(deleted))}")
        self.assertIs(json.loads(tombstone["record"])["tombstone"], True)

    async def test_an_approval_is_its_own_version_ordered_on_arrival(self):
        await self.connect()
        opened = await self.insert("merge_request:open", merge_request())
        approved = await self.insert("merge_request:approval", merge_request("approval"))
        await self.process(FakeGitLab(), opened, approved)
        first, second = await self.records()
        self.assertEqual(first["version"], UPDATED)
        self.assertEqual(second["version"], f"approval:{stamp(await self.received_at(approved))}")

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

    async def test_retry_after_is_stored_and_the_next_call_waits_for_it(self):
        await self.connect()
        gitlab = FakeGitLab(throttle=True)
        await self.process(gitlab, await self.insert("merge_request:open", merge_request()))
        limit = await self.db.fetchrow(
            "select * from rate_limits where source = 'gitlab' and tenant = $1 and bucket = $2", TENANT, ACCOUNT)
        throttled_at, retried_at = [at for at, url in gitlab.calls if "/diffs" in url]
        self.assertGreaterEqual(retried_at - throttled_at, 1.9)
        self.assertEqual(limit["remaining"], 1999)

    async def test_without_its_tenants_connection_a_fetch_waits_and_the_rest_go(self):
        for name, connection in [("not connected", None), ("connected by another tenant", {"tenant": "globex"}),
                                 ("disconnected", {"disconnected": True})]:
            with self.subTest(name):
                await self.db.execute("truncate raw_events, source_records, installations, source_credentials"
                                      " restart identity cascade")
                if connection is not None:
                    await self.connect(**connection)
                gitlab = FakeGitLab()
                needs_fetch = await self.insert("merge_request:open", merge_request())
                no_fetch = await self.insert("issue:open", issue())
                await self.set_aside(gitlab, needs_fetch, no_fetch)
                self.assertEqual(gitlab.calls, [])
                self.assertEqual([r["kind"] for r in await self.records()], ["issue"])

    async def test_a_self_managed_group_is_read_on_its_own_instance(self):
        account = "gitlab.example.com/acme"
        await self.connect(account, base_url="https://gitlab.example.com:8443/gitlab")
        gitlab = FakeGitLab()
        await self.process(gitlab, await self.insert("merge_request:open", merge_request(), account=account))
        self.assertEqual([url.split("?")[0] for _at, url in gitlab.calls],
                         ["https://gitlab.example.com:8443/gitlab/api/v4/projects/42/merge_requests/7/diffs"])

    async def test_a_token_is_never_sent_to_another_instance(self):
        # account_id names gitlab.com, so a credential for another host is refused, not followed
        await self.connect(base_url="https://evil.example")
        gitlab = FakeGitLab()
        await self.set_aside(gitlab, await self.insert("merge_request:open", merge_request()),
                             await self.insert("issue:open", issue()))
        self.assertEqual(gitlab.calls, [])

    async def test_a_refused_token_disconnects_the_group(self):
        await self.connect()
        gitlab = FakeGitLab(refuse=True)
        await self.set_aside(gitlab, await self.insert("merge_request:open", merge_request()),
                             await self.insert("issue:open", issue()))
        self.assertEqual(len(gitlab.calls), 1)
        self.assertIsNotNone(await self.db.fetchval("select disconnected_at from installations"))
        self.assertEqual(await self.db.fetchval("select count(*) from source_credentials"), 0)

    async def test_the_worker_needs_the_api_ring(self):
        worker = GitLabEnrich(Database(database_url()), Credentials(Database(database_url()), {}))
        with self.assertRaisesRegex(ConfigError, "SOURCE_CREDENTIALS_API_KEYS"):
            await worker.start()


if __name__ == "__main__":
    unittest.main()
