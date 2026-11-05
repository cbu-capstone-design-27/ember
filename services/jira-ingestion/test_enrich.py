"""enrich:jira against a real Postgres built by the repo migrations (EMBER-66). Jira is stubbed.

Needs dbmate, the packages in requirements.txt, and a Postgres server it may create and drop a
database on:

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
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

from sqlalchemy import text  # noqa: E402

import atlassian  # noqa: E402
import enrich  # noqa: E402
from pull_test import Response  # noqa: E402
from source_credentials import KeyRing  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
TEST_DB = "ember_test_enrich"
SITE = "photon-synthetic.atlassian.net"
ORIGIN = f"https://{SITE}"
TENANT = "photon"
CLOUD_ID = "0b6e1c39-photon-cloud"
API = atlassian.api_origin(CLOUD_ID)
RING = KeyRing("api", [("test", bytes(range(32)))])
CLIENT = atlassian.OAuthClient("client-id", "client-secret")


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


Call = namedtuple("Call", "at method url headers body")


class Atlassian:
    """pull_test's exchange boundary for connected sites: the token endpoint, photon issues under
    api.atlassian.com for a bearer token only, and webhook refresh. first holds canned answers for the
    first issue GETs, delay slows each issue GET, and token_error fails every refresh with that error."""

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


async def connect_site(engine, *, site: str = SITE, tenant: str = TENANT, expires_at: float | None = None,
                       webhook_ids: list[int] | None = None) -> None:
    """What Connect Jira writes, or rewrites: the installations row and the sealed api credential."""
    credential = {
        "access_token": "access-0", "refresh_token": "refresh-0",
        "expires_at": int(time.time() + 3600 if expires_at is None else expires_at),
        "scope": "read:jira-work read:jira-user manage:jira-webhook offline_access", "cloud_id": CLOUD_ID,
        "webhook_ids": [1001] if webhook_ids is None else webhook_ids, "jql": "project in (10000)",
        "atlassian_account_id": "5be24ad8b1653240376955d2", "atlassian_name": "Integration User",
    }
    sealed = RING.seal("jira", site, credential)
    async with engine.begin() as conn:
        await conn.execute(text("insert into installations (source, account_id, tenant, label, external_id)"
                                " values ('jira', :a, :t, :a, :c)"
                                " on conflict (source, account_id) do update set tenant = excluded.tenant"),
                           {"a": site, "t": tenant, "c": CLOUD_ID})
        await conn.execute(text("insert into source_credentials (source, account_id, kind, key_id, ciphertext)"
                                " values ('jira', :a, 'api', :k, :c)"
                                " on conflict (source, account_id, kind) do update"
                                " set key_id = excluded.key_id, ciphertext = excluded.ciphertext"),
                           {"a": site, "k": sealed.key_id, "c": sealed.ciphertext})


async def stored_credential(engine, site: str = SITE) -> dict | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text("select key_id, ciphertext from source_credentials"
                                       " where source = 'jira' and account_id = :a and kind = 'api'"), {"a": site})).first()
    return None if row is None else RING.open("jira", site, row.key_id, row.ciphertext)


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
            await conn.execute(text(
                "truncate raw_events, source_records, rate_limits, installations restart identity cascade"))
        await connect_site(self.engine)   # photon's site, connected by TENANT, with an hour left on its token

    async def asyncTearDown(self):
        await self.engine.dispose()

    def worker(self, stub: Atlassian | None = None, **kwargs) -> enrich.Worker:
        stub = stub or Atlassian()
        credentials = atlassian.SiteCredentials(self.engine, RING, CLIENT, exchange=stub)
        return enrich.Worker(self.engine, credentials=credentials, exchange=stub, concurrency=4, **kwargs)

    async def insert(self, body: dict, delivery_id: str, account_id: str = SITE, tenant: str = TENANT) -> int:
        async with self.engine.begin() as conn:
            return await conn.scalar(
                text("insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)"
                     " values ('jira', :d, :a, :t, :e, 'webhook', cast(:env as jsonb)) returning id"),
                {"d": delivery_id, "a": account_id, "t": tenant, "e": body["webhookEvent"],
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
        stub = Atlassian()
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
        self.assertEqual(stub.urls(), [f"GET {API}/rest/api/3/issue/10015?fields=%2Aall"])
        self.assertEqual(stub.calls[0].headers["Authorization"], "Bearer access-0")

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
        stub = Atlassian(delay=1.0)
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
        stub = Atlassian(first=[Response(429, {"Retry-After": "1"}, b"{}")])
        row = await self.insert(comment_created(comment_on_new_issue(used)), f"{SITE}:throttled")
        await self.run_until_enriched(self.worker(stub))

        throttled, retried = stub.calls
        self.assertGreaterEqual(retried.at - throttled.at, 1.0)
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
        stub = Atlassian()
        await self.insert(comment_created(comment_on_new_issue(used)), f"{SITE}:held")
        await self.run_until_enriched(self.worker(stub))
        self.assertGreaterEqual(stub.calls[0].at - held_at, 1.4)

    async def test_an_expiring_token_is_refreshed_and_stored_before_it_is_used(self):
        await connect_site(self.engine, expires_at=time.time() + 60)   # inside the 5-minute margin
        stub = Atlassian()
        await self.insert(comment_created(COMMENTS[0]), f"{SITE}:comment")
        await self.run_until_enriched(self.worker(stub))

        self.assertEqual(stub.urls(), [f"POST {atlassian.TOKEN_URL}", f"GET {API}/rest/api/3/issue/10015?fields=%2Aall"])
        self.assertEqual(json.loads(stub.calls[0].body)["refresh_token"], "refresh-0")
        self.assertEqual(stub.calls[1].headers["Authorization"], "Bearer access-1")
        stored = await stored_credential(self.engine)
        self.assertEqual((stored["access_token"], stored["refresh_token"]), ("access-1", "refresh-1"))
        self.assertGreater(stored["expires_at"], time.time() + 3000)
        self.assertEqual(stored["webhook_ids"], [1001])
        self.assertEqual(await self.scalar("select bucket from rate_limits"), ORIGIN)

    async def test_rows_without_their_tenants_token_wait(self):
        """The site is connected by TENANT. A row of the site under another tenant (mapped by hand
        before Connect Jira) gets no token and nothing else: it fails and stays unenriched."""
        stub = Atlassian()
        worker = self.worker(stub, poll_seconds=0.2)
        row = await self.insert(comment_created(COMMENTS[0]), f"{SITE}:other-tenant", tenant="other-tenant")
        task = asyncio.create_task(worker.run())
        start = time.monotonic()
        while row not in worker.failed and time.monotonic() - start < 5:
            await asyncio.sleep(0.02)
        worker.stop()
        await asyncio.wait_for(task, 10)
        self.assertIn(row, worker.failed)
        self.assertEqual(stub.calls, [])
        self.assertIsNone(await self.scalar("select enriched_at from raw_events where id = :id", id=row))

    async def test_a_disconnected_site_has_no_token(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("update installations set disconnected_at = now()"))
        credentials = atlassian.SiteCredentials(self.engine, RING, CLIENT, exchange=Atlassian())
        self.assertIsNone(await credentials.fresh(SITE))
        self.assertEqual(await credentials.connected_sites(), [])

    async def test_concurrent_refreshes_rotate_the_token_once(self):
        await connect_site(self.engine, expires_at=time.time() - 10)
        stub = Atlassian()
        credentials = atlassian.SiteCredentials(self.engine, RING, CLIENT, exchange=stub)
        got = await asyncio.gather(*(credentials.fresh(SITE, tenant=TENANT) for _ in range(4)))
        self.assertEqual(stub.refreshes, 1)
        self.assertEqual({c["refresh_token"] for c in got}, {"refresh-1"})


if __name__ == "__main__":
    unittest.main()
