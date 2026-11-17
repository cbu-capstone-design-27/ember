"""enrich:jira and keep-alive:jira (EMBER-66, EMBER-131, moved in EMBER-160) against a real Postgres.
Atlassian is stubbed at the exchange.

Needs psql, dbmate, this package installed (`pip install services/backend`), and a Postgres server it
may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \\
        python3 services/backend/tests/test_enrich_jira.py

It builds the repo migrations in database ember_test_enrich_jira. The cases carried over from
services/jira-ingestion/test_enrich.py and test_keep_alive.py keep their names.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import asyncpg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

from ember.connectors import CONNECTORS  # noqa: E402
from ember.connectors.jira import atlassian  # noqa: E402
from ember.connectors.jira.enrich import COMMENT_ISSUE, JiraEnrich  # noqa: E402
from ember.connectors.jira.keep_alive import JiraKeepAlive, next_run  # noqa: E402
from ember.core.db import Database  # noqa: E402
from ember.core.errors import ConfigError  # noqa: E402
from ember.core.http import Response  # noqa: E402
from ember.core.settings import Settings  # noqa: E402
from ember.runtime.controller import plan  # noqa: E402
from ember.runtime.deps import Deps  # noqa: E402
from source_credentials import KeyRing  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_enrich_jira"
SITE = "photon-synthetic.atlassian.net"
OTHER_SITE = "second-site.atlassian.net"
ORIGIN = f"https://{SITE}"
TENANT = "photon"
CLOUD_ID = "0b6e1c39-photon-cloud"
API = atlassian.api_origin(CLOUD_ID)
KEY = bytes(range(32))
RING = KeyRing("api", [("test", KEY)])
API_KEYS = "test:" + base64.b64encode(KEY).decode()
CLIENT = atlassian.OAuthClient("client-id", "client-secret")
T0 = datetime(2025, 9, 14, 2, 51, 11, tzinfo=timezone.utc)


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
    issue = ISSUES[COMMENT_ISSUE.search(comment["self"]).group(1)]
    reduced = {"id": issue["id"], "key": issue["key"], "self": issue["self"],
               "fields": {"summary": issue["fields"]["summary"]}}
    return {"timestamp": 1757818572000, "webhookEvent": "comment_created", "comment": comment, "issue": reduced}


def comment_on_new_issue(used: set[str]) -> dict:
    for comment in COMMENTS:
        issue_id = COMMENT_ISSUE.search(comment["self"]).group(1)
        if issue_id not in used:
            used.add(issue_id)
            return comment
    raise AssertionError("no comment on an unused issue")


Call = namedtuple("Call", "at method url headers body")


class Atlassian:
    """The exchange boundary for connected sites: the token endpoint, photon issues under
    api.atlassian.com for a bearer token only, and webhook refresh. `first` holds canned answers for the
    first issue GETs, `delay` slows each issue GET, and `token_error` fails every refresh with that error."""

    def __init__(self, *, delay: float = 0.0, first: list[Response] | None = None,
                 token_error: str | None = None, refresh_status: int = 200) -> None:
        self.delay = delay
        self.first = list(first or [])
        self.token_error = token_error
        self.refresh_status = refresh_status
        self.refreshes = 0
        self.calls: list[Call] = []
        self.called = threading.Event()

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        self.calls.append(Call(time.monotonic(), method, url, headers, body))
        self.called.set()
        if url == atlassian.TOKEN_URL:
            if self.token_error:
                return Response(403, {}, json.dumps({"error": self.token_error}).encode())
            self.refreshes += 1
            return Response(200, {}, json.dumps({
                "access_token": f"access-{self.refreshes}", "refresh_token": f"refresh-{self.refreshes}",
                "expires_in": 3600, "scope": "read:jira-work offline_access"}).encode())
        if method == "PUT" and url == f"{API}/rest/api/3/webhook/refresh":
            return Response(self.refresh_status, {}, b'{"expirationDate": "2026-12-05T09:17:00.000+0000"}')
        match = re.fullmatch(re.escape(API) + r"/rest/api/3/issue/(\d+)\?.*", url)
        if method == "GET" and match and headers.get("Authorization", "").startswith("Bearer access-"):
            time.sleep(self.delay)
            if self.first:
                return self.first.pop(0)
            issue = ISSUES.get(match.group(1))
            if issue is None:
                return Response(404, {}, b"{}")
            return Response(200, {"X-RateLimit-Remaining": "99"}, json.dumps(issue).encode())
        return Response(401, {}, b'{"message": "unexpected call"}')

    def urls(self) -> list[str]:
        return [f"{call.method} {call.url}" for call in self.calls]


async def connect_site(db: asyncpg.Connection, *, site: str = SITE, tenant: str = TENANT,
                       expires_at: float | None = None, webhook_ids: list[int] | None = None) -> None:
    """What Connect Jira writes, or rewrites: the installations row and the sealed api credential."""
    credential = {
        "access_token": "access-0", "refresh_token": "refresh-0",
        "expires_at": int(time.time() + 3600 if expires_at is None else expires_at),
        "scope": "read:jira-work read:jira-user manage:jira-webhook offline_access", "cloud_id": CLOUD_ID,
        "webhook_ids": [1001] if webhook_ids is None else webhook_ids, "jql": "project in (10000)",
        "atlassian_account_id": "5be24ad8b1653240376955d2", "atlassian_name": "Integration User",
    }
    sealed = RING.seal("jira", site, credential)
    await db.execute("insert into installations (source, account_id, tenant, label, external_id)"
                     " values ('jira', $1, $2, $1, $3)"
                     " on conflict (source, account_id) do update set tenant = excluded.tenant", site, tenant, CLOUD_ID)
    await db.execute("insert into source_credentials (source, account_id, kind, key_id, ciphertext)"
                     " values ('jira', $1, 'api', $2, $3)"
                     " on conflict (source, account_id, kind) do update"
                     " set key_id = excluded.key_id, ciphertext = excluded.ciphertext",
                     site, sealed.key_id, sealed.ciphertext)


async def stored_credential(db: asyncpg.Connection, site: str = SITE) -> dict | None:
    row = await db.fetchrow("select key_id, ciphertext from source_credentials"
                            " where source = 'jira' and account_id = $1 and kind = 'api'", site)
    return None if row is None else RING.open("jira", site, row["key_id"], row["ciphertext"])


class DatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, source_records, episodes, rate_limits, installations"
                              " restart identity cascade")
        await connect_site(self.db)   # photon's site, connected by TENANT, with an hour left on its token
        self.engines = []

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        await self.db.close()

    def sites(self, stub: Atlassian) -> atlassian.SiteCredentials:
        engine = Database(database_url()).engine(pool_size=4)
        self.engines.append(engine)
        return atlassian.SiteCredentials(engine, RING, CLIENT, exchange=stub)


class EnrichTest(DatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.running: list[tuple[JiraEnrich, asyncio.Event, asyncio.Task]] = []

    async def asyncTearDown(self):
        for _worker, stopping, task in self.running:
            stopping.set()
            await asyncio.wait_for(task, 10)
        await super().asyncTearDown()

    async def insert(self, body: dict, delivery_id: str, account_id: str = SITE, tenant: str = TENANT,
                     occurred_at: datetime = T0) -> int:
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope, occurred_at)"
            " values ('jira', $1, $2, $3, $4, 'webhook', $5::jsonb, $6) returning id",
            delivery_id, account_id, tenant, body["webhookEvent"], json.dumps({"type": "jira", "body": body}), occurred_at)

    def start(self, stub: Atlassian | None = None, **kwargs) -> tuple[JiraEnrich, asyncio.Event, asyncio.Task]:
        options = {"ring": RING, "client": CLIENT, "exchange": stub or Atlassian(), "poll_seconds": 60, **kwargs}
        worker = JiraEnrich(Database(database_url()), **options)
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

    async def wait_for(self, sql: str, *args, timeout: float = 5.0) -> float:
        """Seconds until sql returns true."""
        start = time.monotonic()
        while not await self.db.fetchval(sql, *args):
            if time.monotonic() - start > timeout:
                raise AssertionError(f"timed out waiting for: {sql}")
            await asyncio.sleep(0.02)
        return time.monotonic() - start

    async def run_until_enriched(self, stub: Atlassian | None = None, **kwargs) -> JiraEnrich:
        worker, stopping, task = self.start(stub, **kwargs)
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")
        stopping.set()
        await asyncio.wait_for(task, 10)
        return worker

    async def wait_retrying(self, worker: JiraEnrich, row: int) -> None:
        start = time.monotonic()
        while row not in worker._retry_at:
            if time.monotonic() - start > 5:
                raise AssertionError(f"raw_events {row} never failed")
            await asyncio.sleep(0.02)

    async def test_issue_and_comment_events_become_source_records(self):
        issue = ISSUES["10001"]
        comment = COMMENTS[0]
        stub = Atlassian()
        await self.insert(issue_created(issue), f"{SITE}:created")
        await self.insert(UPDATED_FIXTURE, "ember-capstone.atlassian.net:updated", "ember-capstone.atlassian.net")
        await self.insert(comment_created(comment), f"{SITE}:comment", occurred_at=T0 + timedelta(minutes=5))
        await self.run_until_enriched(stub)

        rows = {r["object_id"]: r for r in await self.db.fetch(
            "select kind, object_id, version, occurred_at, record from source_records")}
        self.assertEqual(set(rows), {"10001", "10204", "10015/comment/10136"})

        created = rows["10001"]
        self.assertEqual((created["kind"], created["version"]), ("issue", issue["fields"]["updated"]))
        # occurred_at is raw_events.occurred_at, which intake set from the payload (EMBER-63)
        self.assertEqual(created["occurred_at"], T0)
        self.assertEqual(json.loads(created["record"])["payload"]["issue"], issue)

        updated = rows["10204"]
        self.assertEqual(updated["kind"], "issue")
        self.assertEqual(updated["version"], str(UPDATED_FIXTURE["timestamp"]))   # the fixture has no fields.updated
        self.assertEqual(json.loads(updated["record"])["payload"]["changelog"], UPDATED_FIXTURE["changelog"])

        commented = rows["10015/comment/10136"]
        self.assertEqual((commented["kind"], commented["version"]), ("comment", comment["updated"]))
        self.assertEqual(commented["occurred_at"], T0 + timedelta(minutes=5))
        self.assertEqual(json.loads(commented["record"])["issues"]["10015"], ISSUES["10015"])
        self.assertEqual(stub.urls(), [f"GET {API}/rest/api/3/issue/10015?fields=%2Aall"])
        self.assertEqual(stub.calls[0].headers["Authorization"], "Bearer access-0")

    async def test_reprocessing_and_duplicate_versions_leave_one_record(self):
        body = issue_created(ISSUES["10001"])
        await self.insert(body, f"{SITE}:first")
        await self.run_until_enriched()

        await self.db.execute("update raw_events set enriched_at = null")
        await self.insert(body, f"{SITE}:retry-with-a-new-id")
        await self.run_until_enriched()

        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), 1)

    async def test_wakes_on_insert_and_catches_up_on_start(self):
        before = await self.insert(issue_created(ISSUES["10001"]), f"{SITE}:before-start")
        self.start()
        await self.wait_for("select enriched_at is not null from raw_events where id = $1", before)

        after = await self.insert(issue_created(ISSUES["10002"]), f"{SITE}:while-running")
        elapsed = await self.wait_for("select exists (select 1 from source_records where raw_event_id = $1)", after)
        self.assertLess(elapsed, 1.0)

    async def test_stopping_finishes_the_row_in_flight(self):
        stub = Atlassian(delay=1.0)
        row = await self.insert(comment_created(COMMENTS[0]), f"{SITE}:slow")
        _worker, stopping, task = self.start(stub)
        await asyncio.to_thread(stub.called.wait, 5)
        stopping.set()
        await asyncio.wait_for(task, 10)

        # The fetch ends at least stub.delay after the row was received, and enriched_at can't be earlier
        received_to_enriched = await self.db.fetchval(
            "select extract(epoch from enriched_at - received_at) from raw_events where id = $1", row)
        self.assertGreaterEqual(received_to_enriched, stub.delay)
        self.assertEqual(await self.db.fetchval("select count(*) from source_records where raw_event_id = $1", row), 1)

    async def test_retry_after_sets_next_allowed_at_and_calls_wait_for_it(self):
        used: set[str] = set()
        stub = Atlassian(first=[Response(429, {"Retry-After": "1"}, b"{}")])
        row = await self.insert(comment_created(comment_on_new_issue(used)), f"{SITE}:throttled")
        await self.run_until_enriched(stub)

        throttled, retried = stub.calls
        self.assertGreaterEqual(retried.at - throttled.at, 1.0)
        # The 429 came after the row was received, so next_allowed_at is at least 1 s past that
        pushed = await self.db.fetchval(
            "select extract(epoch from l.next_allowed_at - r.received_at) from rate_limits l, raw_events r"
            " where l.source = 'jira' and l.tenant = $1 and l.bucket = $2 and r.id = $3", TENANT, ORIGIN, row)
        self.assertGreaterEqual(pushed, 1.0)

        # The next call waits for next_allowed_at even with no 429 of its own
        await self.db.execute("update rate_limits set next_allowed_at = now() + interval '1.5 seconds'")
        held_at = time.monotonic()
        stub = Atlassian()
        await self.insert(comment_created(comment_on_new_issue(used)), f"{SITE}:held")
        await self.run_until_enriched(stub)
        self.assertGreaterEqual(stub.calls[0].at - held_at, 1.4)

    async def test_an_expiring_token_is_refreshed_and_stored_before_it_is_used(self):
        await connect_site(self.db, expires_at=time.time() + 60)   # inside the 5-minute margin
        stub = Atlassian()
        await self.insert(comment_created(COMMENTS[0]), f"{SITE}:comment")
        await self.run_until_enriched(stub)

        self.assertEqual(stub.urls(), [f"POST {atlassian.TOKEN_URL}", f"GET {API}/rest/api/3/issue/10015?fields=%2Aall"])
        self.assertEqual(json.loads(stub.calls[0].body)["refresh_token"], "refresh-0")
        self.assertEqual(stub.calls[1].headers["Authorization"], "Bearer access-1")
        stored = await stored_credential(self.db)
        self.assertEqual((stored["access_token"], stored["refresh_token"]), ("access-1", "refresh-1"))
        self.assertGreater(stored["expires_at"], time.time() + 3000)
        self.assertEqual(stored["webhook_ids"], [1001])
        self.assertEqual(await self.db.fetchval("select bucket from rate_limits"), ORIGIN)

    async def test_rows_without_their_tenants_token_wait(self):
        """The site is connected by TENANT. A row of the site under another tenant (mapped by hand
        before Connect Jira) gets no token and nothing else: it fails and stays unenriched."""
        stub = Atlassian()
        row = await self.insert(comment_created(COMMENTS[0]), f"{SITE}:other-tenant", tenant="other-tenant")
        worker, _stopping, _task = self.start(stub)
        await self.wait_retrying(worker, row)
        self.assertEqual(stub.calls, [])
        self.assertIsNone(await self.db.fetchval("select enriched_at from raw_events where id = $1", row))

    async def test_a_refused_token_sets_the_row_aside_for_an_hour(self):
        await connect_site(self.db, expires_at=time.time() - 10)
        row = await self.insert(comment_created(COMMENTS[0]), f"{SITE}:refused")
        worker, _stopping, _task = self.start(Atlassian(token_error="invalid_grant"))
        await self.wait_retrying(worker, row)
        self.assertGreater(worker._retry_at[row] - time.monotonic(), 3000)  # Permanent, not retried with backoff
        self.assertIsNotNone(await stored_credential(self.db))  # the keep-alive disconnects, not enrich

    async def test_without_the_ring_or_the_client_only_rows_that_need_a_call_wait(self):
        stub = Atlassian()
        needs_call = await self.insert(comment_created(COMMENTS[0]), f"{SITE}:comment")
        no_call = await self.insert(issue_created(ISSUES["10001"]), f"{SITE}:created")
        worker, _stopping, _task = self.start(stub, client=None, missing="JIRA_OAUTH_CLIENT_ID is not set")
        await self.wait_for("select enriched_at is not null from raw_events where id = $1", no_call)
        await self.wait_retrying(worker, needs_call)
        self.assertEqual(stub.calls, [])

    async def test_an_issue_jira_no_longer_shows_is_left_out(self):
        stub = Atlassian(first=[Response(404, {}, b"{}")])
        await self.insert(comment_created(COMMENTS[0]), f"{SITE}:comment")
        await self.run_until_enriched(stub)
        record = json.loads(await self.db.fetchval("select record from source_records"))
        self.assertNotIn("issues", record)

    async def test_a_disconnected_site_has_no_token(self):
        await self.db.execute("update installations set disconnected_at = now()")
        sites = self.sites(Atlassian())
        self.assertIsNone(await sites.fresh(SITE))
        self.assertEqual(await sites.connected_sites(), [])

    async def test_concurrent_refreshes_rotate_the_token_once(self):
        await connect_site(self.db, expires_at=time.time() - 10)
        stub = Atlassian()
        sites = self.sites(stub)
        got = await asyncio.gather(*(sites.fresh(SITE, tenant=TENANT) for _ in range(4)))
        self.assertEqual(stub.refreshes, 1)
        self.assertEqual({c["refresh_token"] for c in got}, {"refresh-1"})


class KeepAliveTest(DatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.execute("truncate installations cascade")
        self.components: list[JiraKeepAlive] = []

    async def asyncTearDown(self):
        for component in self.components:
            await component.close()
        await super().asyncTearDown()

    async def component(self, stub: Atlassian, **kwargs) -> JiraKeepAlive:
        component = JiraKeepAlive(Database(database_url()), ring=RING, client=CLIENT, exchange=stub, **kwargs)
        self.components.append(component)
        await component.start()
        return component

    async def keep_alive(self, stub: Atlassian) -> int:
        component = await self.component(stub)
        self.assertTrue(await component.run_once())
        return component.last["failed"]

    async def test_refreshes_the_token_and_extends_the_webhooks(self):
        await connect_site(self.db, webhook_ids=[1001, 1002])   # an hour left: refreshed anyway
        stub = Atlassian()
        self.assertEqual(await self.keep_alive(stub), 0)

        self.assertEqual(stub.urls(), [f"POST {atlassian.TOKEN_URL}", f"PUT {API}/rest/api/3/webhook/refresh"])
        self.assertEqual(stub.calls[1].headers["Authorization"], "Bearer access-1")
        self.assertEqual(json.loads(stub.calls[1].body), {"webhookIds": [1001, 1002]})
        self.assertEqual((await stored_credential(self.db))["refresh_token"], "refresh-1")

    async def test_a_refused_refresh_disconnects_the_site(self):
        await connect_site(self.db)
        stub = Atlassian(token_error="invalid_grant")
        self.assertEqual(await self.keep_alive(stub), 0)

        self.assertEqual(stub.urls(), [f"POST {atlassian.TOKEN_URL}"])
        self.assertIsNotNone(await self.db.fetchval("select disconnected_at from installations where account_id = $1", SITE))
        self.assertIsNone(await stored_credential(self.db))
        # The next run has nothing to do
        stub = Atlassian()
        self.assertEqual(await self.keep_alive(stub), 0)
        self.assertEqual(stub.calls, [])

    async def test_other_failures_count_and_leave_the_site_connected(self):
        await connect_site(self.db)
        await connect_site(self.db, site=OTHER_SITE, tenant="other-tenant")

        self.assertEqual(await self.keep_alive(Atlassian(refresh_status=404)), 2)
        # A refusal that isn't invalid_grant (our client, say) is ours to fix, not the owner's
        self.assertEqual(await self.keep_alive(Atlassian(token_error="unauthorized_client")), 2)
        self.assertEqual(await self.db.fetchval("select count(*) from installations where disconnected_at is null"), 2)
        self.assertIsNotNone(await stored_credential(self.db, OTHER_SITE))

    async def test_one_replica_runs_at_a_time(self):
        await connect_site(self.db)
        stub = Atlassian()
        component = await self.component(stub)
        await self.db.fetchval("select pg_advisory_lock(hashtext('ember:keep-alive:jira'))")  # another replica
        self.assertFalse(await component.run_once())
        await self.db.fetchval("select pg_advisory_unlock(hashtext('ember:keep-alive:jira'))")
        self.assertEqual(stub.calls, [])
        self.assertTrue(await component.run_once())

    async def test_runs_once_a_day_at_its_time(self):
        now = datetime(2026, 11, 15, 10, 17, tzinfo=timezone.utc)
        self.assertEqual(next_run(now - timedelta(minutes=1)), now)
        self.assertEqual(next_run(now), now + timedelta(days=1))  # just ran: tomorrow

        await connect_site(self.db)
        stub = Atlassian()
        soon = datetime.now(timezone.utc) + timedelta(seconds=1)
        component = await self.component(stub, at=soon.time())
        stopping = asyncio.Event()
        task = asyncio.create_task(component.run(stopping))
        await asyncio.to_thread(stub.called.wait, 5)
        while component.last is None:
            await asyncio.sleep(0.02)
        self.assertEqual(component.status()["next_run"], next_run(soon, soon.time()).isoformat())
        stopping.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(component.last["sites"], 1)


class SettingsTest(unittest.IsolatedAsyncioTestCase):
    """How SOURCE_CREDENTIALS_API_KEYS and JIRA_* become the two components, or why one is disabled."""

    def plan(self, **env: str):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("JIRA_", "EMBER_", "SOURCE_CREDENTIALS_"))}
        with mock.patch.dict(os.environ, {**clean, "DATABASE_URL": database_url(), **env}, clear=True):
            return plan(Deps.build(Settings.load()), CONNECTORS)

    def parts(self, **env: str) -> dict:
        return {c.name: c for c in self.plan(**env).components if c.name.endswith(":jira")}

    async def test_the_keep_alive_names_what_it_is_missing(self):
        cases = [({}, "SOURCE_CREDENTIALS_API_KEYS, JIRA_OAUTH_CLIENT_ID, JIRA_OAUTH_CLIENT_SECRET are not set"),
                 ({"SOURCE_CREDENTIALS_API_KEYS": API_KEYS, "JIRA_OAUTH_CLIENT_SECRET": "secret"},
                  "JIRA_OAUTH_CLIENT_ID is not set")]
        for env, reason in cases:
            with self.subTest(reason):
                parts = self.parts(**env)
                self.assertEqual(set(parts), {"enrich:jira", "keep-alive:jira"})
                with self.assertRaisesRegex(ConfigError, f"^{reason}$"):
                    await parts["keep-alive:jira"].start()
                self.assertEqual(parts["enrich:jira"].missing, reason)

    async def test_an_unreadable_api_key_ring_disables_both(self):
        built = self.plan(SOURCE_CREDENTIALS_API_KEYS="test:not base64!")
        self.assertIn("SOURCE_CREDENTIALS_API_KEYS can't be read", built.disabled["workers:jira"])
        self.assertNotIn("not base64!", json.dumps(built.disabled).replace("'test'", ""))
        names = [c.name for c in built.components]
        self.assertNotIn("enrich:jira", names)
        self.assertIn("enrich:github", names)  # every other source's workers still run

    async def test_settings_reach_the_worker(self):
        parts = self.parts(SOURCE_CREDENTIALS_API_KEYS=API_KEYS, JIRA_OAUTH_CLIENT_ID="id",
                           JIRA_OAUTH_CLIENT_SECRET="secret", JIRA_ENRICH_CONCURRENCY="2", JIRA_ENRICH_BATCH="5")
        worker = parts["enrich:jira"]
        self.assertEqual((worker.concurrency, worker.batch, worker.client), (2, 5, atlassian.OAuthClient("id", "secret")))
        self.assertEqual(worker.ring.kind, "api")


if __name__ == "__main__":
    unittest.main()
