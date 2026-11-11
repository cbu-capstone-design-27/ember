"""intake against a real Postgres (EMBER-62). Nothing here calls GitHub, GitLab, Jira or Slack.

Needs psql, dbmate, the packages in requirements.txt, and a Postgres server it may create and drop
a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/intake/test_intake.py

It builds the repo migrations in database ember_test_intake.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
import warnings
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import uvicorn
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families
from starlette.responses import PlainTextResponse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

import intake  # noqa: E402
import jwt  # noqa: E402
from adapters import Delivery, Unauthorized  # noqa: E402
from adapters.github import RECOMMENDED_WEBHOOK_EVENTS, GitHubAdapter  # noqa: E402
from adapters.gitlab import GitLabAdapter  # noqa: E402
from adapters.jira import JiraAdapter  # noqa: E402
from adapters.slack import SlackAdapter  # noqa: E402
from models import Installation, RawEvent, SourceCredential  # noqa: E402
from source_credentials import KeyRing  # noqa: E402
from validate import assert_valid  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_intake"
SECRET = "test-webhook-secret"
FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "github.json").read_text())["body"]
SLACK_SECRET = "test-signing-secret"
SLACK_FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "slack.json").read_text())["body"]
JIRA_SECRET = "test-jira-webhook-secret"
JIRA_FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "jira.json").read_text())["body"]
JIRA_SITE = "ember-capstone.atlassian.net"  # the host of the fixture's issue.self
JIRA_OAUTH_SECRET = "test-jira-3lo-client-secret"
GITLAB_KEY = b"test-gitlab-signing-key-0123456789"
GITLAB_SIGNING_TOKEN = "whsec_" + base64.b64encode(GITLAB_KEY).decode()
GITLAB_SECRET_TOKEN = "test-gitlab-secret-token"
GITLAB_FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "gitlab.json").read_text())["body"]
GITLAB_ACCOUNT = "gitlab.com/acme"  # X-Gitlab-Instance's host and the fixture's top-level group
# EMBER-149: the webhook key ring, and the signing token Connect GitLab stored for gitlab.com/acme
WEBHOOK_KEYS = "test:" + base64.b64encode(bytes(range(32))).decode()
WEBHOOK_RING = KeyRing.parse("webhook", WEBHOOK_KEYS)
ACME_KEY = b"acme-own-signing-key-from-connect-gitlab"
ACME_SIGNING_TOKEN = "whsec_" + base64.b64encode(ACME_KEY).decode()


def database_url() -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def github(event: str, payload: dict, delivery_id: str, secret: str = SECRET) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": sign(body, secret),
                  "Content-Type": "application/json"}


def slack(payload: dict | bytes, secret: str = SLACK_SECRET, timestamp: int | None = None,
          **headers: str) -> tuple[bytes, dict]:
    """A delivery as Slack signs it: v0=HMAC-SHA256 of "v0:<timestamp>:<body>"."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    sent = str(int(time.time()) if timestamp is None else timestamp)
    signature = "v0=" + hmac.new(secret.encode(), f"v0:{sent}:".encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Slack-Request-Timestamp": sent, "X-Slack-Signature": signature,
                  "Content-Type": "application/json", **headers}


def jira(payload: dict | bytes, identifier: str = "wh-1", secret: str = JIRA_SECRET, **headers: str) -> tuple[bytes, dict]:
    """A delivery as a Jira admin webhook with a secret sends it: X-Hub-Signature, sha256=HMAC of the body."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return body, {"X-Atlassian-Webhook-Identifier": identifier, "X-Hub-Signature": sign(body, secret),
                  "Content-Type": "application/json", **headers}


def jira_token(secret: str = JIRA_OAUTH_SECRET, algorithm: str = "HS256", **claims) -> str:
    """A token as Atlassian signs a dynamic webhook delivery's: HS256 with the 3LO app's client secret."""
    now = int(time.time())
    with warnings.catch_warnings():  # the test secrets are shorter than a real one
        warnings.simplefilter("ignore", jwt.warnings.InsecureKeyLengthWarning)
        payload = {key: value for key, value in {"iss": "client-id", "iat": now, "exp": now + 180, **claims}.items()
                   if value is not None}  # a claim given as None is left out
        return jwt.encode(payload, secret, algorithm=algorithm)


def jira_oauth(payload: dict | bytes, identifier: str = "wh-1", token: str | None = None, **headers: str) -> tuple[bytes, dict]:
    """A delivery as a dynamic webhook from Connect Jira sends it: Authorization: Bearer <JWT>, no X-Hub-Signature."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return body, {"X-Atlassian-Webhook-Identifier": identifier, "Authorization": f"Bearer {token or jira_token()}",
                  "Content-Type": "application/json", **headers}


def gitlab(payload: dict | bytes, webhook_id: str = "msg-1", key: bytes = GITLAB_KEY, timestamp: int | None = None,
           **headers: str) -> tuple[bytes, dict]:
    """A delivery as a GitLab webhook with a signing token sends it: v1,<base64 HMAC of "<id>.<ts>.<body>">."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    sent = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(key, f"{webhook_id}.{sent}.".encode() + body, hashlib.sha256).digest()
    return body, {"webhook-id": webhook_id, "Idempotency-Key": webhook_id, "webhook-timestamp": sent,
                  "webhook-signature": "v1," + base64.b64encode(digest).decode(), "X-Gitlab-Instance": "https://gitlab.com",
                  "X-Gitlab-Event": "Merge Request Hook", "Content-Type": "application/json", **headers}


def request(url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class FakeTeams:
    """A second source, to show that adding one is adding an adapter. Not Teams' real rules."""

    name = "teams"

    def handshake(self, delivery: Delivery):
        token = delivery.query.get("validationToken")
        return PlainTextResponse(token) if token else None

    def verify(self, delivery: Delivery) -> None:
        if delivery.json.get("clientState") != "expected":
            raise Unauthorized("clientState does not match")

    def delivery_id(self, delivery: Delivery) -> str:
        return hashlib.sha256(delivery.body).hexdigest()

    def account_id(self, delivery: Delivery) -> str | None:
        return delivery.json.get("tenantId")

    def event_name(self, delivery: Delivery) -> str:
        if delivery.json["changeType"] == "boom":
            raise RuntimeError(f"a bug that quotes the payload: {delivery.json}")
        return delivery.json["changeType"]

    def occurred_at(self, delivery: Delivery) -> None:
        return None


class Server:
    """create_app served by uvicorn on a free port, inside the test's event loop."""

    def __init__(self, url: str, registry: CollectorRegistry | None = None, *, timeout: float = intake.DB_TIMEOUT,
                 deadline: float = intake.STORE_DEADLINE, gitlab_shared: str | None = GITLAB_SIGNING_TOKEN,
                 gitlab_stored: bool = False) -> None:
        """gitlab_stored: verify GitLab with the tokens Connect GitLab stored (EMBER-149); gitlab_shared: the shared token."""
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        engine = intake.create_engine(url, timeout)
        tokens = intake.StoredSigningTokens(engine, WEBHOOK_RING) if gitlab_stored else None
        app = intake.create_app(engine, {"github": GitHubAdapter(SECRET), "slack": SlackAdapter(SLACK_SECRET),
                                         "jira": JiraAdapter(JIRA_SECRET, JIRA_OAUTH_SECRET),
                                         "gitlab": GitLabAdapter(gitlab_shared or "", tokens=tokens),
                                         "teams": FakeTeams()},
                                registry, deadline=deadline)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))

    async def __aenter__(self) -> Server:
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.should_exit = True
        await self.task

    async def post(self, path: str, body: bytes, headers: dict) -> tuple[int, str]:
        return await asyncio.to_thread(request, self.base + path, body, headers)

    async def get(self, path: str) -> tuple[int, str]:
        return await asyncio.to_thread(request, self.base + path)


def setUpModule():
    run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
        "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
    run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")


def tearDownModule():
    run("psql", "-X", "-q", "-d", os.environ["PGTEST_URL"], "-c", f"drop database if exists {DB} with (force)")


class IntakeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, installations restart identity cascade")
        await self.db.execute("insert into installations (source, account_id, tenant)"
                              " values ('github', '77', 'acme'), ('gitlab', '77', 'not-this-one'),"
                              " ('slack', 'T012WORKSPACE', 'acme'), ('jira', $1, 'acme'), ('gitlab', $2, 'acme')",
                              JIRA_SITE, GITLAB_ACCOUNT)
        self.registry = CollectorRegistry()
        self.intake = await Server(database_url(), self.registry).__aenter__()

    async def asyncTearDown(self):
        await self.intake.__aexit__()
        await self.db.close()

    async def deliver(self, event: str, payload: dict, delivery_id: str, **kwargs) -> tuple[int, str]:
        body, headers = github(event, payload, delivery_id, **kwargs)
        return await self.intake.post("/webhook/github", body, headers)

    async def rows(self):
        return await self.db.fetch("select * from raw_events order by id")

    async def counts(self) -> dict[tuple[str, str], float]:
        """intake_deliveries_total by (source, result), read from /metrics."""
        status, text = await self.intake.get("/metrics")
        self.assertEqual(status, 200)
        return {(s.labels["source"], s.labels["result"]): s.value
                for family in text_string_to_metric_families(text) if family.name == "intake_deliveries"
                for s in family.samples if s.name == "intake_deliveries_total"}

    async def test_models_match_the_migrated_schema(self):
        for model in (RawEvent, Installation, SourceCredential):
            table = model.__table__
            rows = await self.db.fetch(
                "select column_name, is_nullable = 'YES' as nullable from information_schema.columns"
                " where table_schema = 'public' and table_name = $1", table.name)
            self.assertEqual({(c.name, c.nullable) for c in table.columns},
                             {(r["column_name"], r["nullable"]) for r in rows}, table.name)

    async def test_signed_delivery_is_stored_as_it_arrived(self):
        payload = {**FIXTURE, "installation": {"id": 77}}
        self.assertEqual(await self.deliver("issues", payload, "d-1"), (200, '{"status":"stored"}'))

        (row,) = await self.rows()
        self.assertEqual(
            (row["source"], row["delivery_id"], row["account_id"], row["tenant"], row["event_name"], row["origin"]),
            ("github", "d-1", "77", "acme", "issues:opened", "webhook"))
        self.assertEqual(row["occurred_at"], datetime(2026, 9, 23, 22, 15, tzinfo=timezone.utc))
        envelope = json.loads(row["envelope"])
        self.assertEqual(envelope, {"type": "github", "body": payload})
        assert_valid(envelope)
        self.assertIsNone(row["enriched_at"])

    async def test_unmapped_installation_is_stored_without_a_tenant(self):
        await self.deliver("issues", {**FIXTURE, "installation": {"id": 99}}, "unmapped")
        await self.deliver("issues", FIXTURE, "no-installation")
        self.assertEqual([(r["account_id"], r["tenant"]) for r in await self.rows()], [("99", None), (None, None)])

    async def test_disconnected_installation_is_stored_without_a_tenant(self):
        # ADR 0010: a disconnected account's deliveries are stored as an unmapped account's are
        await self.db.execute("update installations set disconnected_at = now() where source = 'github' and account_id = '77'")
        await self.deliver("issues", {**FIXTURE, "installation": {"id": 77}}, "after-disconnect")
        await self.db.execute("update installations set disconnected_at = null where source = 'github' and account_id = '77'")
        await self.deliver("issues", {**FIXTURE, "installation": {"id": 77}}, "after-reconnect")
        self.assertEqual([(r["delivery_id"], r["tenant"]) for r in await self.rows()],
                         [("after-disconnect", None), ("after-reconnect", "acme")])

    async def test_redelivery_is_a_no_op_and_wakes_no_one(self):
        notified: list[str] = []
        await self.db.add_listener("raw_events", lambda *args: notified.append(args[3]))
        payload = {**FIXTURE, "installation": {"id": 77}}
        self.assertEqual(await self.deliver("issues", payload, "same"), (200, '{"status":"stored"}'))
        self.assertEqual(await self.deliver("issues", payload, "same"), (200, '{"status":"duplicate"}'))
        await asyncio.sleep(0.3)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(notified, ["github"])

    async def test_occurred_at_follows_githubs_ordering_rule(self):
        # github-webhooks.md, "Ordering value". None: falls back to received_at
        cases = [
            ("issues", {"action": "edited", "issue": {"updated_at": "2026-09-24T08:00:00Z"}}, "2026-09-24T08:00:00+00:00"),
            ("pull_request", {"action": "closed", "pull_request": {"updated_at": "2026-09-24T08:30:00Z"}}, "2026-09-24T08:30:00+00:00"),
            ("pull_request_review", {"action": "submitted", "review": {"submitted_at": "2026-09-24T09:00:00Z"}}, "2026-09-24T09:00:00+00:00"),
            ("release", {"action": "published", "release": {"published_at": "2026-09-24T10:00:00Z", "created_at": "2026-09-20T00:00:00Z"}}, "2026-09-24T10:00:00+00:00"),
            ("release", {"action": "created", "release": {"published_at": None, "created_at": "2026-09-20T00:00:00Z"}}, "2026-09-20T00:00:00+00:00"),
            ("push", {"ref": "refs/heads/main", "repository": {"pushed_at": 1790326800}}, "2026-09-25T09:00:00+00:00"),
            ("issue_comment", {"action": "deleted", "comment": {"updated_at": "2026-09-01T00:00:00Z"}}, None),
            ("pull_request_review", {"action": "dismissed", "review": {"submitted_at": "2026-09-01T00:00:00Z"}}, None),
            ("label", {"action": "created", "label": {"name": "bug"}}, None),
            ("create", {"ref": "main", "ref_type": "branch"}, None),
            ("ping", {"zen": "Keep it logically awesome."}, None),
            ("issues", {"action": "edited", "issue": {"updated_at": "not a time"}}, None),
            ("issues", {"action": "edited", "issue": {"updated_at": None, "created_at": "2026-09-01T00:00:00Z"}}, None),
            ("issues", {"action": "edited", "issue": {"updated_at": "0001-01-01T00:00:00+05:00"}}, None),
            ("issues", {"action": "edited", "issue": {"updated_at": "2026-09-24T08:00:00+02:00"}}, "2026-09-24T06:00:00+00:00"),
            # An edit keeps the review's submit time and the release's publish time, so it gets received_at
            ("pull_request_review", {"action": "edited", "review": {"submitted_at": "2026-09-01T00:00:00Z"}}, None),
            ("release", {"action": "edited", "release": {"published_at": "2026-09-01T00:00:00Z"}}, None),
            ("push", {"ref": "refs/heads/main", "repository": {"pushed_at": 0}}, None),
        ]
        for n, (event, payload, _expected) in enumerate(cases):
            self.assertEqual((await self.deliver(event, payload, f"order-{n}"))[0], 200)
        rows = await self.rows()
        for (event, payload, expected), row in zip(cases, rows, strict=True):
            with self.subTest(event=row["event_name"]):
                want = row["received_at"] if expected is None else datetime.fromisoformat(expected)
                self.assertEqual(row["occurred_at"], want)

    async def test_rejected_deliveries_store_nothing(self):
        payload = {**FIXTURE, "installation": {"id": 77}}
        body, headers = github("issues", payload, "rejected")
        cases = [
            ("bad signature", "/webhook/github", body, {**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64}, 401),
            ("no signature", "/webhook/github", body, {k: v for k, v in headers.items() if k != "X-Hub-Signature-256"}, 401),
            ("another secret", "/webhook/github", *github("issues", payload, "rejected", secret="other"), 401),
            ("unknown source", "/webhook/bitbucket", body, headers, 404),
            ("not an object", "/webhook/github", b"[]", {**headers, "X-Hub-Signature-256": sign(b"[]")}, 400),
            ("not json", "/webhook/github", b"{", {**headers, "X-Hub-Signature-256": sign(b"{")}, 400),
            ("no delivery id", "/webhook/github", body, {k: v for k, v in headers.items() if k != "X-GitHub-Delivery"}, 400),
            ("no event", "/webhook/github", body, {k: v for k, v in headers.items() if k != "X-GitHub-Event"}, 400),
            ("long delivery id", "/webhook/github", body, {**headers, "X-GitHub-Delivery": "d" * 8000}, 400),
            ("NaN", "/webhook/github", b'{"n":NaN}', {**headers, "X-Hub-Signature-256": sign(b'{"n":NaN}')}, 400),
            ("deep nesting", "/webhook/github", b"[" * 100_000, {**headers, "X-Hub-Signature-256": sign(b"[" * 100_000)}, 400),
        ]
        for name, path, case_body, case_headers, status in cases:
            with self.subTest(name):
                self.assertEqual((await self.intake.post(path, case_body, case_headers))[0], status)
        self.assertEqual(await self.rows(), [])

    async def test_body_over_8_mib_is_refused_before_it_is_read(self):
        at_limit = b'{"pad":"' + b"x" * (intake.MAX_BODY_BYTES - 10) + b'"}'
        self.assertEqual(len(at_limit), intake.MAX_BODY_BYTES)
        headers = {"X-GitHub-Event": "ping", "X-GitHub-Delivery": "at-limit", "X-Hub-Signature-256": sign(at_limit)}
        self.assertEqual((await self.intake.post("/webhook/github", at_limit, headers))[0], 200)

        # Declared too large: answered from the headers, so the client never has to send the body
        reader, writer = await asyncio.open_connection("127.0.0.1", self.intake.port)
        writer.write(f"POST /webhook/github HTTP/1.1\r\nHost: x\r\nContent-Length: {intake.MAX_BODY_BYTES + 1}\r\n"
                     "X-GitHub-Event: ping\r\nX-GitHub-Delivery: too-large\r\n\r\n".encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        self.assertTrue(response.startswith(b"HTTP/1.1 413 "), response)
        self.assertEqual([r["delivery_id"] for r in await self.rows()], ["at-limit"])

    async def test_chunked_body_over_8_mib_is_refused(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.intake.port)
        writer.write(b"POST /webhook/github HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
                     b"X-GitHub-Event: ping\r\nX-GitHub-Delivery: chunked\r\n\r\n")
        chunk = b"x" * (1024 * 1024)
        response = asyncio.create_task(reader.readline())
        with contextlib.suppress(ConnectionError):
            for _ in range(intake.MAX_BODY_BYTES // len(chunk) + 1):
                writer.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        status = await asyncio.wait_for(response, 5)
        self.assertTrue(status.startswith(b"HTTP/1.1 413 "), status)
        writer.close()
        self.assertEqual(await self.rows(), [])

    async def test_client_that_hangs_up_mid_body_is_counted_without_a_traceback(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.intake.port)
        writer.write(b"POST /webhook/github HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n" + b"{" * 10)
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
        for _ in range(100):
            if (await self.counts()).get(("github", "disconnected")) == 1:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(await self.counts(), {("github", "disconnected"): 1})

    async def test_payload_postgres_cannot_store_is_refused_without_logging_it(self):
        payload = {**FIXTURE, "installation": {"id": 77}, "note": "secret-title \u0000"}
        with self.assertLogs("intake", "WARNING") as logs:
            self.assertEqual(await self.deliver("issues", payload, "nul"),
                             (400, '{"error":"postgres cannot store this payload"}'))
        self.assertNotIn("secret-title", "\n".join(logs.output))
        self.assertEqual(await self.rows(), [])

    async def test_an_unexpected_error_is_counted_and_not_logged_with_the_payload(self):
        with self.assertLogs("intake", "ERROR") as logs:
            status = await self.intake.post("/webhook/teams", json.dumps(
                {"clientState": "expected", "changeType": "boom", "secret": "secret-title"}).encode(), {})
        self.assertEqual(status, (500, '{"error":"internal error"}'))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("secret-title", "\n".join(logs.output))
        self.assertEqual((await self.counts())[("teams", "error")], 1)

    async def lock_raw_events(self) -> asyncpg.Connection:
        """A connection holding raw_events locked, so every insert waits on it."""
        locker = await asyncpg.connect(database_url())
        await locker.execute("begin; lock table raw_events in access exclusive mode")
        return locker

    async def test_slow_postgres_answers_503_inside_githubs_10_seconds(self):
        for name, limits in (("statement timeout", {"timeout": 1}), ("request deadline", {"timeout": 30, "deadline": 1})):
            with self.subTest(name):
                async with Server(database_url(), **limits) as server:
                    locker = await self.lock_raw_events()
                    try:
                        body, headers = github("issues", FIXTURE, f"slow-{name}")
                        started = time.monotonic()
                        self.assertEqual((await server.post("/webhook/github", body, headers))[0], 503)
                        self.assertLess(time.monotonic() - started, 3)
                    finally:
                        await locker.close()
                    self.assertEqual((await server.post("/webhook/github", body, headers))[0], 200)

    async def test_backend_terminated_mid_insert_answers_503(self):
        locker = await self.lock_raw_events()
        try:
            body, headers = github("issues", FIXTURE, "terminated")
            reply = asyncio.create_task(self.intake.post("/webhook/github", body, headers))
            for _ in range(200):
                waiting = await self.db.fetchval(
                    "select pid from pg_stat_activity where datname = $1 and wait_event_type = 'Lock'", DB)
                if waiting:
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(waiting, "the insert never waited on the lock")
            await self.db.execute("select pg_terminate_backend($1)", waiting)  # as a Postgres restart does
            self.assertEqual((await asyncio.wait_for(reply, 5))[0], 503)
        finally:
            await locker.close()
        self.assertEqual((await self.intake.post("/webhook/github", body, headers))[0], 200)

    async def test_postgres_out_of_connections_answers_503(self):
        # A role allowed no connections is refused with 53300 while connecting, as a full Postgres refuses
        await self.db.execute("drop role if exists intake_test_full")
        await self.db.execute("create role intake_test_full login password 'full' connection limit 0")
        try:
            parts = urlsplit(database_url())
            full = urlunsplit(parts._replace(netloc=f"intake_test_full:full@{parts.hostname}:{parts.port}"))
            async with Server(full) as server:
                body, headers = github("issues", FIXTURE, "out-of-connections")
                self.assertEqual(await server.post("/webhook/github", body, headers),
                                 (503, '{"error":"postgres unavailable"}'))
                self.assertEqual((await server.get("/health"))[0], 503)
        finally:
            await self.db.execute("drop role if exists intake_test_full")

    async def test_postgres_unreachable_answers_503(self):
        down = urlunsplit(urlsplit(database_url())._replace(netloc=f"postgres@127.0.0.1:{free_port()}"))
        async with Server(down) as server:
            body, headers = github("issues", FIXTURE, "while-down")
            self.assertEqual(await server.post("/webhook/github", body, headers),
                             (503, '{"error":"postgres unavailable"}'))
            self.assertEqual((await server.get("/health"))[0], 503)
        self.assertEqual((await self.intake.get("/health")), (200, '{"status":"ok"}'))

    async def test_metrics_count_each_result(self):
        payload = {**FIXTURE, "installation": {"id": 77}}
        await self.deliver("issues", payload, "m-1")
        await self.deliver("issues", payload, "m-1")
        await self.deliver("issues", payload, "m-2", secret="other")
        await self.intake.post("/webhook/no-such-source", b"{}", {})
        await self.intake.post("/webhook/teams?validationToken=t", b"", {})

        self.assertEqual(await self.counts(), {("github", "stored"): 1, ("github", "duplicate"): 1, ("github", "unauthorized"): 1,
                                  ("unknown", "unknown_source"): 1, ("teams", "handshake"): 1})

    async def deliver_slack(self, payload: dict | bytes, **kwargs) -> tuple[int, str]:
        return await self.intake.post("/webhook/slack", *slack(payload, **kwargs))

    async def test_slack_url_verification_is_answered_and_not_stored(self):
        check = {"token": "t", "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P", "type": "url_verification"}
        self.assertEqual(await self.deliver_slack(check),
                         (200, '{"challenge":"3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"}'))
        # Slack signs the check too, so an unsigned one is refused rather than echoed
        self.assertEqual((await self.deliver_slack(check, secret="other"))[0], 401)
        self.assertEqual((await self.deliver_slack({"type": "url_verification"}))[0], 400)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.counts(), {("slack", "handshake"): 1, ("slack", "unauthorized"): 1,
                                               ("slack", "bad_request"): 1})

    async def test_signed_slack_event_is_stored_as_it_arrived(self):
        self.assertEqual(await self.deliver_slack(SLACK_FIXTURE), (200, '{"status":"stored"}'))

        (row,) = await self.rows()
        self.assertEqual(
            (row["source"], row["delivery_id"], row["account_id"], row["tenant"], row["event_name"], row["origin"]),
            ("slack", "Ev012EVENT", "T012WORKSPACE", "acme", "message", "webhook"))
        # event_ts to the microsecond, as the ts string has it
        self.assertEqual(row["occurred_at"], datetime(2024, 5, 29, 16, 26, 41, 200, tzinfo=timezone.utc))
        envelope = json.loads(row["envelope"])
        self.assertEqual(envelope, {"type": "slack", "body": SLACK_FIXTURE})
        assert_valid(envelope)
        self.assertIsNone(row["enriched_at"])

    async def test_slack_retry_is_a_no_op_and_an_unmapped_workspace_waits(self):
        self.assertEqual(await self.deliver_slack(SLACK_FIXTURE), (200, '{"status":"stored"}'))
        # Slack retries with the same event_id after 0 s, 1 and 5 minutes when it got no 200 in 3 s
        self.assertEqual(await self.deliver_slack(SLACK_FIXTURE, **{"X-Slack-Retry-Num": "1",
                                                                     "X-Slack-Retry-Reason": "http_timeout"}),
                         (200, '{"status":"duplicate"}'))
        await self.deliver_slack({**SLACK_FIXTURE, "team_id": "T0OTHER", "event_id": "Ev0OTHER"})
        self.assertEqual([(r["delivery_id"], r["tenant"]) for r in await self.rows()],
                         [("Ev012EVENT", "acme"), ("Ev0OTHER", None)])

    async def test_slack_throttle_notice_is_stored_once_per_minute(self):
        notice = {"token": "t", "type": "app_rate_limited", "team_id": "T012WORKSPACE",
                  "minute_rate_limited": 1717000020, "api_app_id": "A012APP"}
        self.assertEqual(await self.deliver_slack(notice), (200, '{"status":"stored"}'))
        self.assertEqual(await self.deliver_slack(notice), (200, '{"status":"duplicate"}'))
        (row,) = await self.rows()
        self.assertEqual((row["delivery_id"], row["account_id"], row["tenant"], row["event_name"], row["occurred_at"]),
                         ("rate_limited:T012WORKSPACE:1717000020", "T012WORKSPACE", "acme", "app_rate_limited",
                          datetime(2024, 5, 29, 16, 27, tzinfo=timezone.utc)))

    async def test_occurred_at_follows_slacks_ordering_rule(self):
        # slack-webhooks.md, "Ordering value": event_ts, then ts, then event_time. None: received_at
        def event(n: int, inner: dict, **wrapper) -> dict:
            return {"type": "event_callback", "team_id": "T012WORKSPACE", "event_id": f"Ev{n}", **wrapper, "event": inner}

        cases = [
            ({"type": "message", "subtype": "message_changed", "ts": "1717000009.000001", "event_ts": "1717000010.123456"},
             {"event_time": 1717000099}, "message:message_changed", "2024-05-29T16:26:50.123456+00:00"),
            ({"type": "message", "subtype": "message_deleted", "ts": "1717000011.5"}, {"event_time": 1717000099},
             "message:message_deleted", "2024-05-29T16:26:51.500000+00:00"),
            ({"type": "channel_rename", "channel": {"id": "C1", "name": "new"}}, {"event_time": 1717000012},
             "channel_rename", "2024-05-29T16:26:52+00:00"),
            ({"type": "member_joined_channel", "user": "U1", "channel": "C1"}, {}, "member_joined_channel", None),
            ({"type": "message", "event_ts": "not a ts", "ts": "1717000013.000000"}, {}, "message",
             "2024-05-29T16:26:53+00:00"),
            ({"type": "message", "event_ts": " 1717000015.0", "ts": "nope"}, {"event_time": 1717000016}, "message",
             "2024-05-29T16:26:56+00:00"),
            ({"type": "message", "event_ts": "99999999999999999999.0"}, {}, "message", None),
            ({"type": "message", "event_ts": True, "ts": "1717000014.000000"}, {}, "message",
             "2024-05-29T16:26:54+00:00"),
        ]
        for n, (inner, wrapper, _name, _expected) in enumerate(cases):
            self.assertEqual((await self.deliver_slack(event(n, inner, **wrapper)))[0], 200)
        rows = await self.rows()
        for (_inner, _wrapper, name, expected), row in zip(cases, rows, strict=True):
            with self.subTest(event=row["delivery_id"]):
                self.assertEqual(row["event_name"], name)
                want = row["received_at"] if expected is None else datetime.fromisoformat(expected)
                self.assertEqual(row["occurred_at"], want)

    async def test_rejected_slack_deliveries_store_nothing(self):
        body, headers = slack(SLACK_FIXTURE)
        now = int(time.time())
        no_id = {k: v for k, v in SLACK_FIXTURE.items() if k != "event_id"}
        cases = [
            ("bad signature", body, {**headers, "X-Slack-Signature": "v0=" + "0" * 64}, 401),
            ("no signature", body, {k: v for k, v in headers.items() if k != "X-Slack-Signature"}, 401),
            ("no timestamp", body, {k: v for k, v in headers.items() if k != "X-Slack-Request-Timestamp"}, 401),
            ("another secret", *slack(SLACK_FIXTURE, secret="other"), 401),
            ("signed for another body", body + b" ", headers, 401),
            ("replayed after 5 minutes", *slack(SLACK_FIXTURE, timestamp=now - 301), 401),
            # Not now + 301: now is rounded down and the request takes time, so the gap intake sees can be 300 s
            ("from the future", *slack(SLACK_FIXTURE, timestamp=now + 310), 401),
            ("timestamp not a number", body, {**headers, "X-Slack-Request-Timestamp": "-1"}, 401),
            ("timestamp a superscript digit", body, {**headers, "X-Slack-Request-Timestamp": "\u00b2"}, 401),
            ("timestamp too long", body, {**headers, "X-Slack-Request-Timestamp": "9" * 5000}, 401),
            ("signed with GitHub's scheme", body, {**headers, "X-Slack-Signature": sign(body)}, 401),
            ("not an object", *slack(b"[]"), 400),
            ("not json", *slack(b"{"), 400),
            ("no event_id", *slack(no_id), 400),
            ("long event_id", *slack({**SLACK_FIXTURE, "event_id": "E" * 8000}), 400),
            ("not an Events API body", *slack({"type": "block_actions", "team_id": "T012WORKSPACE"}), 400),
            ("throttle notice without its minute", *slack({"type": "app_rate_limited", "team_id": "T012WORKSPACE"}), 400),
        ]
        for name, case_body, case_headers, status in cases:
            with self.subTest(name):
                self.assertEqual((await self.intake.post("/webhook/slack", case_body, case_headers))[0], status)
        self.assertEqual(await self.rows(), [])

    async def deliver_jira(self, payload: dict | bytes, identifier: str = "wh-1", **kwargs) -> tuple[int, str]:
        return await self.intake.post("/webhook/jira", *jira(payload, identifier, **kwargs))

    async def test_signed_jira_delivery_is_stored_as_it_arrived(self):
        self.assertEqual(await self.deliver_jira(JIRA_FIXTURE), (200, '{"status":"stored"}'))

        (row,) = await self.rows()
        self.assertEqual(
            (row["source"], row["delivery_id"], row["account_id"], row["tenant"], row["event_name"], row["origin"]),
            ("jira", f"{JIRA_SITE}:wh-1", JIRA_SITE, "acme", "jira:issue_updated", "webhook"))
        # The fixture's issue has no fields.updated, so the body's timestamp (epoch ms) orders it
        self.assertEqual(row["occurred_at"], datetime(2025, 9, 23, 23, 9, 8, tzinfo=timezone.utc))
        envelope = json.loads(row["envelope"])
        self.assertEqual(envelope, {"type": "jira", "body": JIRA_FIXTURE})
        assert_valid(envelope)
        self.assertIsNone(row["enriched_at"])

    async def test_jira_retry_is_a_no_op_and_an_unmapped_site_waits(self):
        self.assertEqual(await self.deliver_jira(JIRA_FIXTURE), (200, '{"status":"stored"}'))
        # Jira retries with the same identifier, up to 5 times, on anything but a 200
        self.assertEqual(await self.deliver_jira(JIRA_FIXTURE, **{"X-Atlassian-Webhook-Retry": "1",
                                                                   "X-Atlassian-Webhook-Flow": "Primary"}),
                         (200, '{"status":"duplicate"}'))
        # The identifier is unique only within a site, so another site's same identifier is a new delivery
        other = {**JIRA_FIXTURE, "issue": {**JIRA_FIXTURE["issue"], "self": "https://other.atlassian.net/rest/api/3/issue/1"}}
        self.assertEqual(await self.deliver_jira(other), (200, '{"status":"stored"}'))
        self.assertEqual([(r["delivery_id"], r["account_id"], r["tenant"]) for r in await self.rows()],
                         [(f"{JIRA_SITE}:wh-1", JIRA_SITE, "acme"),
                          ("other.atlassian.net:wh-1", "other.atlassian.net", None)])

    async def test_jira_site_comes_from_the_events_own_entity(self):
        site = f"https://{JIRA_SITE}/rest/api/3"
        cases = [
            ("comment", {"webhookEvent": "comment_created", "timestamp": 1,
                         "comment": {"id": "5", "self": f"{site}/issue/10204/comment/5"},
                         "issue": {"id": "10204", "self": "https://reduced.example/issue/10204"}}, JIRA_SITE),
            ("worklog", {"webhookEvent": "worklog_deleted", "timestamp": 1,
                         "worklog": {"id": "7", "issueId": "10204", "self": f"{site}/issue/10204/worklog/7"}}, JIRA_SITE),
            ("link", {"webhookEvent": "issuelink_created", "timestamp": 1,
                      "issueLink": {"id": "9", "self": f"{site}/issueLink/9"}}, JIRA_SITE),
            ("no self anywhere", {"webhookEvent": "jira:issue_updated", "timestamp": 1, "issue": {"id": "1"}}, None),
            ("malformed self", {"webhookEvent": "jira:issue_updated", "timestamp": 1,
                                "issue": {"self": "https://[::1"}}, None),
        ]
        for n, (_name, payload, _site) in enumerate(cases):
            self.assertEqual((await self.deliver_jira(payload, f"wh-{n}"))[0], 200)
        for (name, _payload, expected), row in zip(cases, await self.rows(), strict=True):
            with self.subTest(name):
                self.assertEqual(row["account_id"], expected)
                self.assertTrue(row["delivery_id"].startswith(f"{expected or 'unknown'}:wh-"))

    async def test_occurred_at_follows_jiras_ordering_rule(self):
        # jira-webhooks.md, "Ordering value": the entity's updated, then attachment.created, then timestamp
        ts = 1760389511123  # 2025-10-13T21:05:11.123Z
        updated = "2026-10-13T14:05:11.123-0700"  # 2026-10-13T21:05:11.123Z
        cases = [
            ({"webhookEvent": "jira:issue_updated", "issue": {"fields": {"updated": updated}}}, "2026-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "jira:issue_created", "issue": {"fields": {"updated": "2026-10-13T21:05:11.000+0000"}}},
             "2026-10-13T21:05:11+00:00"),
            ({"webhookEvent": "comment_updated", "comment": {"updated": updated}}, "2026-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "worklog_created", "worklog": {"updated": updated}}, "2026-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "attachment_created", "attachment": {"created": updated}}, "2026-10-13T21:05:11.123+00:00"),
            # A deleted entity's updated is its last edit, not the delete
            ({"webhookEvent": "jira:issue_deleted", "issue": {"fields": {"updated": updated}}}, "2025-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "comment_deleted", "comment": {"updated": updated}}, "2025-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "issuelink_created", "issueLink": {"id": 1}}, "2025-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "jira:issue_updated", "issue": {"fields": {"updated": "not a time"}}}, "2025-10-13T21:05:11.123+00:00"),
            ({"webhookEvent": "jira:issue_updated", "issue": {"fields": {"updated": "2026-10-13T14:05:11"}}},
             "2025-10-13T21:05:11.123+00:00"),
        ]
        for n, (payload, _expected) in enumerate(cases):
            self.assertEqual((await self.deliver_jira({**payload, "timestamp": ts}, f"wh-{n}"))[0], 200)
        # No readable time at all: received_at
        self.assertEqual((await self.deliver_jira({"webhookEvent": "jira:issue_updated", "timestamp": "soon"}, "wh-none"))[0], 200)
        self.assertEqual((await self.deliver_jira({"webhookEvent": "jira:issue_updated", "timestamp": True}, "wh-bool"))[0], 200)
        rows = await self.rows()
        for (payload, expected), row in zip(cases, rows, strict=False):
            with self.subTest(payload["webhookEvent"], delivery=row["delivery_id"]):
                self.assertEqual(row["event_name"], payload["webhookEvent"])
                self.assertEqual(row["occurred_at"], datetime.fromisoformat(expected))
        for row in rows[len(cases):]:
            self.assertEqual(row["occurred_at"], row["received_at"])

    async def test_rejected_jira_deliveries_store_nothing(self):
        body, headers = jira(JIRA_FIXTURE)
        cases = [
            ("bad signature", body, {**headers, "X-Hub-Signature": "sha256=" + "0" * 64}, 401),
            ("no signature", body, {k: v for k, v in headers.items() if k != "X-Hub-Signature"}, 401),
            ("another secret", *jira(JIRA_FIXTURE, secret="other"), 401),
            ("signed for another body", body + b" ", headers, 401),
            ("sha1 signature", body, {**headers, "X-Hub-Signature": "sha1=" + hmac.new(
                JIRA_SECRET.encode(), body, hashlib.sha1).hexdigest()}, 401),
            ("signed with GitHub's secret", *jira(JIRA_FIXTURE, secret=SECRET), 401),
            ("no identifier", body, {k: v for k, v in headers.items() if k != "X-Atlassian-Webhook-Identifier"}, 400),
            ("long identifier", *jira(JIRA_FIXTURE, "i" * 8000), 400),
            ("no webhookEvent", *jira({k: v for k, v in JIRA_FIXTURE.items() if k != "webhookEvent"}), 400),
            ("webhookEvent not a string", *jira({**JIRA_FIXTURE, "webhookEvent": 3}), 400),
            ("not an object", *jira(b"[]"), 400),
            ("not json", *jira(b"{"), 400),
        ]
        for name, case_body, case_headers, status in cases:
            with self.subTest(name):
                self.assertEqual((await self.intake.post("/webhook/jira", case_body, case_headers))[0], status)
        self.assertEqual(await self.rows(), [])

    async def test_dynamic_webhook_delivery_is_stored_like_an_admin_ones(self):
        # Connect Jira's webhook (EMBER-131): the same body, signed by a bearer token instead of the body's HMAC
        self.assertEqual(await self.intake.post("/webhook/jira", *jira_oauth(JIRA_FIXTURE)), (200, '{"status":"stored"}'))
        self.assertEqual(await self.intake.post("/webhook/jira", *jira_oauth(JIRA_FIXTURE)), (200, '{"status":"duplicate"}'))
        (row,) = await self.rows()
        self.assertEqual((row["delivery_id"], row["account_id"], row["tenant"], row["event_name"]),
                         (f"{JIRA_SITE}:wh-1", JIRA_SITE, "acme", "jira:issue_updated"))

    async def test_rejected_dynamic_webhook_deliveries_store_nothing(self):
        now = int(time.time())
        body, headers = jira_oauth(JIRA_FIXTURE)
        unsigned = jwt.encode({"exp": now + 60}, None, algorithm="none")
        cases = [
            ("another secret", jira_token("another-client-secret")),
            ("signed with the admin webhook's secret", jira_token(JIRA_SECRET)),
            ("expired", jira_token(exp=now - 120)),
            ("issued in the future", jira_token(iat=now + 600)),
            ("no exp", jira_token(exp=None)),
            ("alg none", unsigned),
            ("HS512", jira_token(algorithm="HS512")),
            ("not a JWT", "abc"),
        ]
        for name, token in cases:
            with self.subTest(name):
                self.assertEqual((await self.intake.post("/webhook/jira", *jira_oauth(JIRA_FIXTURE, token=token)))[0], 401)
        for name, auth in (("Basic scheme", "Basic " + jira_token()), ("empty bearer", "Bearer ")):
            with self.subTest(name):
                self.assertEqual((await self.intake.post("/webhook/jira", body, {**headers, "Authorization": auth}))[0], 401)
        # A bearer token is never checked as the admin webhook's, even with a valid X-Hub-Signature beside it
        signed = jira(JIRA_FIXTURE)[1]
        self.assertEqual((await self.intake.post("/webhook/jira", body, {**signed, "Authorization": "Bearer x"}))[0], 401)
        self.assertEqual(await self.rows(), [])

    async def deliver_gitlab(self, payload: dict | bytes, webhook_id: str = "msg-1", **kwargs) -> tuple[int, str]:
        return await self.intake.post("/webhook/gitlab", *gitlab(payload, webhook_id, **kwargs))

    async def test_signed_gitlab_delivery_is_stored_as_it_arrived(self):
        self.assertEqual(await self.deliver_gitlab(GITLAB_FIXTURE), (200, '{"status":"stored"}'))

        (row,) = await self.rows()
        self.assertEqual(
            (row["source"], row["delivery_id"], row["account_id"], row["tenant"], row["event_name"], row["origin"]),
            ("gitlab", "msg-1", GITLAB_ACCOUNT, "acme", "merge_request:open", "webhook"))
        # The fixture's merge request has no updated_at, so it is ordered by arrival
        self.assertEqual(row["occurred_at"], row["received_at"])
        envelope = json.loads(row["envelope"])
        self.assertEqual(envelope, {"type": "gitlab", "body": GITLAB_FIXTURE})
        assert_valid(envelope)
        self.assertIsNone(row["enriched_at"])

    async def test_gitlab_retry_is_a_no_op_and_an_unmapped_group_waits(self):
        self.assertEqual(await self.deliver_gitlab(GITLAB_FIXTURE), (200, '{"status":"stored"}'))
        # A retry or a manual Resend keeps webhook-id and is signed again with a new timestamp
        self.assertEqual(await self.deliver_gitlab(GITLAB_FIXTURE, timestamp=int(time.time()) - 60),
                         (200, '{"status":"duplicate"}'))
        # Idempotency-Key is the same id under its older name, used when webhook-id is missing
        legacy = GitLabAdapter("", GITLAB_SECRET_TOKEN)
        self.assertEqual(legacy.delivery_id(Delivery({"idempotency-key": "msg-1"}, {}, b"{}")), "msg-1")
        other = {**GITLAB_FIXTURE, "project": {**GITLAB_FIXTURE["project"], "path_with_namespace": "other-group/app"}}
        self.assertEqual(await self.deliver_gitlab(other, "msg-2"), (200, '{"status":"stored"}'))
        self.assertEqual([(r["delivery_id"], r["account_id"], r["tenant"]) for r in await self.rows()],
                         [("msg-1", GITLAB_ACCOUNT, "acme"), ("msg-2", "gitlab.com/other-group", None)])

    async def test_gitlab_account_is_the_instance_and_top_level_group(self):
        project = {"id": 7, "path_with_namespace": "acme/platform/api"}
        cases = [
            ("subgroup project", {"X-Gitlab-Instance": "https://gitlab.com"}, project, GITLAB_ACCOUNT),
            ("self-managed instance", {"X-Gitlab-Instance": "https://GitLab.Example.com:8443"}, project,
             "gitlab.example.com/acme"),
            ("host without a scheme", {"X-Gitlab-Instance": "gitlab.com"}, project, GITLAB_ACCOUNT),
            ("no instance header", {"X-Gitlab-Instance": ""}, project, None),
            ("malformed instance", {"X-Gitlab-Instance": "https://[::1"}, project, None),
            ("no project", {}, None, None),
        ]
        for n, (_name, headers, proj, _account) in enumerate(cases):
            payload = {"object_kind": "push", **({"project": proj} if proj else {})}
            self.assertEqual((await self.deliver_gitlab(payload, f"msg-{n}", **headers))[0], 200)
        for (name, _headers, _proj, expected), row in zip(cases, await self.rows(), strict=True):
            with self.subTest(name):
                self.assertEqual(row["account_id"], expected)

    async def test_occurred_at_follows_gitlabs_ordering_rule(self):
        # gitlab-webhooks.md, "Ordering value": object_attributes.updated_at, except pushes and approvals
        updated = "2026-01-16T05:56:25.123Z"
        cases = [
            ({"object_kind": "merge_request", "object_attributes": {"action": "update", "updated_at": updated}},
             "merge_request:update", "2026-01-16T05:56:25.123+00:00"),
            # Self-managed instances before GitLab 19.0
            ({"object_kind": "issue", "object_attributes": {"action": "close", "updated_at": "2026-01-16 05:56:25 UTC"}},
             "issue:close", "2026-01-16T05:56:25+00:00"),
            ({"object_kind": "work_item", "object_attributes": {"action": "open", "updated_at": updated}},
             "work_item:open", "2026-01-16T05:56:25.123+00:00"),
            ({"object_kind": "note", "object_attributes": {"updated_at": "2026-01-16T06:56:25+01:00"}},
             "note", "2026-01-16T05:56:25+00:00"),
        ]
        by_arrival = [
            # GitLab doesn't say approvals bump updated_at, a push has only commit times, and a delete has none
            ({"object_kind": "merge_request", "object_attributes": {"action": "approved", "updated_at": updated}},
             "merge_request:approved"),
            ({"object_kind": "push", "commits": [{"timestamp": updated}]}, "push"),
            ({"object_kind": "push", "after": "0" * 40, "commits": []}, "push"),
            ({"object_kind": "tag_push", "object_attributes": {"updated_at": updated}}, "tag_push"),
            ({"object_kind": "issue", "object_attributes": {"action": "update", "updated_at": "not a time"}},
             "issue:update"),
            ({"object_kind": "issue", "object_attributes": {"action": "update", "updated_at": "2026-01-16T05:56:25"}},
             "issue:update"),
            ({"object_kind": "issue", "object_attributes": {"action": "update", "updated_at": 1768542985}},
             "issue:update"),
        ]
        payloads = [payload for payload, *_ in cases + by_arrival]
        for n, payload in enumerate(payloads):
            self.assertEqual((await self.deliver_gitlab(payload, f"msg-{n}"))[0], 200)
        rows = await self.rows()
        for (payload, event, expected), row in zip(cases, rows, strict=False):
            with self.subTest(event):
                self.assertEqual(row["event_name"], event)
                self.assertEqual(row["occurred_at"], datetime.fromisoformat(expected))
        for (payload, event), row in zip(by_arrival, rows[len(cases):], strict=True):
            with self.subTest(event, delivery=row["delivery_id"]):
                self.assertEqual(row["event_name"], event)
                self.assertEqual(row["occurred_at"], row["received_at"])

    async def test_rejected_gitlab_deliveries_store_nothing(self):
        body, headers = gitlab(GITLAB_FIXTURE)

        def without(*names: str) -> dict:
            return {k: v for k, v in headers.items() if k not in names}

        cases = [
            ("bad signature", body, {**headers, "webhook-signature": "v1," + base64.b64encode(b"0" * 32).decode()}, 401),
            ("no signature", body, without("webhook-signature"), 401),
            ("no timestamp", body, without("webhook-timestamp"), 401),
            ("no webhook-id", body, without("webhook-id"), 401),
            ("another key", *gitlab(GITLAB_FIXTURE, key=b"other"), 401),
            ("signed for another body", body + b" ", headers, 401),
            ("signed for another id", body, {**headers, "webhook-id": "msg-2"}, 401),
            ("timestamp over 5 minutes old", *gitlab(GITLAB_FIXTURE, timestamp=int(time.time()) - 301), 401),
            ("timestamp over 5 minutes ahead", *gitlab(GITLAB_FIXTURE, timestamp=int(time.time()) + 310), 401),
            ("timestamp not a number", body, {**headers, "webhook-timestamp": "soon"}, 401),
            ("only the legacy token", body, {**without("webhook-signature"), "X-Gitlab-Token": GITLAB_SECRET_TOKEN}, 401),
            ("long webhook-id", *gitlab(GITLAB_FIXTURE, "i" * 8000), 400),
            ("no object_kind", *gitlab({k: v for k, v in GITLAB_FIXTURE.items() if k != "object_kind"}), 400),
            ("object_kind not a string", *gitlab({**GITLAB_FIXTURE, "object_kind": 3}), 400),
            ("not an object", *gitlab(b"[]"), 400),
            ("not json", *gitlab(b"{"), 400),
        ]
        for name, case_body, case_headers, status in cases:
            with self.subTest(name):
                self.assertEqual((await self.intake.post("/webhook/gitlab", case_body, case_headers))[0], status)
        self.assertEqual(await self.rows(), [])

    async def test_gitlab_legacy_secret_token_is_checked_with_or_without_signing(self):
        body, headers = gitlab(GITLAB_FIXTURE)
        legacy = GitLabAdapter("", GITLAB_SECRET_TOKEN)
        both = GitLabAdapter(GITLAB_SIGNING_TOKEN, GITLAB_SECRET_TOKEN)
        token = {"x-gitlab-token": GITLAB_SECRET_TOKEN}
        # verify is async since EMBER-149, which may look up the account's own token
        await legacy.verify(Delivery(token, {}, body))
        await both.verify(Delivery({**{k.lower(): v for k, v in headers.items()}, **token}, {}, body))
        cases = [
            ("legacy, wrong token", legacy, {"x-gitlab-token": "other"}),
            ("legacy, no token", legacy, {}),
            ("both, signed but no token", both, {k.lower(): v for k, v in headers.items()}),
            ("both, token but unsigned", both, token),
        ]
        for name, adapter, case_headers in cases:
            with self.subTest(name), self.assertRaises(Unauthorized):
                await adapter.verify(Delivery(case_headers, {}, body))

    async def test_adding_a_source_is_adding_an_adapter(self):
        self.assertEqual(await self.intake.post("/webhook/teams?validationToken=abc", b"", {}), (200, "abc"))
        body = json.dumps({"clientState": "expected", "tenantId": "t-1", "changeType": "created"}).encode()
        self.assertEqual(await self.intake.post("/webhook/teams", body, {}), (200, '{"status":"stored"}'))
        self.assertEqual((await self.intake.post("/webhook/teams", b'{"clientState":"wrong"}', {}))[0], 401)

        (row,) = await self.rows()
        self.assertEqual((row["source"], row["delivery_id"], row["account_id"], row["event_name"]),
                         ("teams", hashlib.sha256(body).hexdigest(), "t-1", "created"))
        assert_valid(json.loads(row["envelope"]))

    async def test_recommended_events_are_documented(self):
        readme = (HERE / "README.md").read_text()
        for event in RECOMMENDED_WEBHOOK_EVENTS:
            self.assertIn(f"`{event}`", readme)


class ConnectedGitLabTest(unittest.IsolatedAsyncioTestCase):
    """EMBER-149: a group connected from the web app is verified with its own signing token."""

    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, installations restart identity cascade")
        await self.db.execute("insert into installations (source, account_id, tenant) values"
                              " ('gitlab', $1, 'acme'), ('gitlab', 'gitlab.com/globex', 'globex'),"
                              " ('gitlab', 'gitlab.com/initech', 'initech')", GITLAB_ACCOUNT)
        await self.store_token(GITLAB_ACCOUNT, ACME_SIGNING_TOKEN)
        await self.store_token("gitlab.com/globex", "whsec_" + base64.b64encode(b"globex-own-key").decode())
        self.servers: list[Server] = []

    async def asyncTearDown(self):
        for server in self.servers:
            await server.__aexit__()
        await self.db.close()

    async def store_token(self, account: str, token: str, *, source: str = "gitlab") -> None:
        sealed = WEBHOOK_RING.seal(source, account, {"signing_tokens": [token]})
        await self.db.execute("insert into source_credentials (source, account_id, kind, key_id, ciphertext)"
                              " values ($1, $2, 'webhook', $3, $4)", source, account, sealed.key_id, sealed.ciphertext)

    async def serve(self, **kwargs) -> Server:
        server = await Server(database_url(), gitlab_stored=True, **kwargs).__aenter__()
        self.servers.append(server)
        return server

    @staticmethod
    def of(group: str) -> dict:
        return {**GITLAB_FIXTURE, "project": {**GITLAB_FIXTURE["project"], "path_with_namespace": f"{group}/app"}}

    async def rows(self):
        return [(r["delivery_id"], r["account_id"], r["tenant"])
                for r in await self.db.fetch("select * from raw_events order by id")]

    async def test_a_connected_group_lands_under_its_tenant_with_its_own_token(self):
        server = await self.serve(gitlab_shared=None)
        self.assertEqual(await server.post("/webhook/gitlab", *gitlab(GITLAB_FIXTURE, "own", key=ACME_KEY)),
                         (200, '{"status":"stored"}'))
        self.assertEqual(await self.rows(), [("own", GITLAB_ACCOUNT, "acme")])

    async def test_each_webhook_of_a_group_verifies_with_its_own_token(self):
        # On Free every project has its own webhook, and GitLab generates each one's signing token
        second_key = b"acme-second-project-webhook-key"
        sealed = WEBHOOK_RING.seal("gitlab", GITLAB_ACCOUNT, {"signing_tokens": [
            ACME_SIGNING_TOKEN, "whsec_" + base64.b64encode(second_key).decode()]})
        await self.db.execute("update source_credentials set key_id = $1, ciphertext = $2 where account_id = $3",
                              sealed.key_id, sealed.ciphertext, GITLAB_ACCOUNT)
        server = await self.serve(gitlab_shared=None)
        for n, key in enumerate((ACME_KEY, second_key)):
            self.assertEqual((await server.post("/webhook/gitlab", *gitlab(GITLAB_FIXTURE, f"hook-{n}", key=key)))[0], 200)
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(GITLAB_FIXTURE, "other", key=b"x" * 32)))[0], 401)
        self.assertEqual(await self.rows(), [("hook-0", GITLAB_ACCOUNT, "acme"), ("hook-1", GITLAB_ACCOUNT, "acme")])

    async def test_a_group_connected_before_its_webhook_keeps_the_shared_token_meanwhile(self):
        # Connected from the web app, no webhook token pasted yet: the hand-made webhook still lands
        sealed = WEBHOOK_RING.seal("gitlab", GITLAB_ACCOUNT, {"signing_tokens": []})
        await self.db.execute("update source_credentials set key_id = $1, ciphertext = $2 where account_id = $3",
                              sealed.key_id, sealed.ciphertext, GITLAB_ACCOUNT)
        server = await self.serve()
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(GITLAB_FIXTURE, "shared")))[0], 200)
        self.assertEqual(await self.rows(), [("shared", GITLAB_ACCOUNT, "acme")])

    async def test_only_the_accounts_own_token_verifies_it(self):
        server = await self.serve()  # the shared token is configured too
        cases = [
            # The shared token no longer verifies a group that has its own
            ("shared token", gitlab(GITLAB_FIXTURE, "shared")),
            # Another connected group's token, on a body naming acme
            ("another group's token", gitlab(GITLAB_FIXTURE, "globex", key=b"globex-own-key")),
            # acme's token, on a body naming globex: the account picks the token, so it fails globex's
            ("signed by acme, names globex", gitlab(self.of("globex"), "spoof", key=ACME_KEY)),
            ("unsigned", (gitlab(GITLAB_FIXTURE)[0], {"webhook-id": "x", "X-Gitlab-Instance": "https://gitlab.com"})),
        ]
        for name, (body, headers) in cases:
            with self.subTest(name):
                self.assertEqual((await server.post("/webhook/gitlab", body, headers))[0], 401)
        self.assertEqual(await self.rows(), [])

    async def test_an_account_with_no_signing_token_is_rejected(self):
        server = await self.serve(gitlab_shared=None)
        # initech is mapped but has no stored token; other-group is neither
        for n, group in enumerate(("initech", "other-group")):
            with self.subTest(group):
                for key in (ACME_KEY, GITLAB_KEY):
                    self.assertEqual((await server.post("/webhook/gitlab", *gitlab(self.of(group), f"{n}-{key[:4]}",
                                                                                     key=key)))[0], 401)
        # No account at all: nothing to look up
        no_project = {k: v for k, v in GITLAB_FIXTURE.items() if k != "project"}
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(no_project, "none", key=ACME_KEY)))[0], 401)
        self.assertEqual(await self.rows(), [])

    async def test_the_shared_token_still_serves_groups_set_up_by_hand(self):
        server = await self.serve()
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(self.of("initech"), "by-hand")))[0], 200)
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(self.of("other-group"), "unmapped")))[0], 200)
        self.assertEqual(await self.rows(), [("by-hand", "gitlab.com/initech", "initech"),
                                             ("unmapped", "gitlab.com/other-group", None)])

    async def test_disconnect_deletes_the_token_so_the_webhook_is_refused(self):
        server = await self.serve(gitlab_shared=None)
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(GITLAB_FIXTURE, "before", key=ACME_KEY)))[0], 200)
        # What the web app's Disconnect does (lib/connections.ts disconnectAccount)
        await self.db.execute("update installations set disconnected_at = now() where source = 'gitlab' and account_id = $1",
                              GITLAB_ACCOUNT)
        await self.db.execute("delete from source_credentials where source = 'gitlab' and account_id = $1", GITLAB_ACCOUNT)
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(GITLAB_FIXTURE, "after", key=ACME_KEY)))[0], 401)
        self.assertEqual(await self.rows(), [("before", GITLAB_ACCOUNT, "acme")])

    async def test_a_token_stored_for_another_account_or_source_does_not_open(self):
        # A row copied to another account fails AES-GCM's additional data, and is a bug, not a 401
        sealed = WEBHOOK_RING.seal("gitlab", GITLAB_ACCOUNT, {"signing_tokens": [ACME_SIGNING_TOKEN]})
        await self.db.execute("update source_credentials set key_id = $1, ciphertext = $2"
                              " where source = 'gitlab' and account_id = 'gitlab.com/globex'", sealed.key_id, sealed.ciphertext)
        server = await self.serve(gitlab_shared=None)
        status, _ = await server.post("/webhook/gitlab", *gitlab(self.of("globex"), "copied", key=ACME_KEY))
        self.assertEqual(status, 500)
        # Other sources' webhook rows are never read as GitLab's
        await self.db.execute("insert into installations (source, account_id, tenant) values ('jira', 'gitlab.com/hooli', 'hooli')")
        await self.store_token("gitlab.com/hooli", ACME_SIGNING_TOKEN, source="jira")
        self.assertEqual((await server.post("/webhook/gitlab", *gitlab(self.of("hooli"), "jira-row", key=ACME_KEY)))[0], 401)
        self.assertEqual(await self.rows(), [])

    def test_the_adapter_needs_some_way_to_verify(self):
        with self.assertRaisesRegex(ValueError, "is required"):
            GitLabAdapter()
        GitLabAdapter(tokens=intake.StoredSigningTokens(None, WEBHOOK_RING))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "webhook key ring"):
            intake.StoredSigningTokens(None, KeyRing.parse("api", WEBHOOK_KEYS))  # type: ignore[arg-type]


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    """`python3 intake.py` as the container runs it."""

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_WEBHOOK_SECRET", "SLACK_SIGNING_SECRET", "JIRA_WEBHOOK_SECRET", "DATABASE_URL",
                             "SOURCE_CREDENTIALS_WEBHOOK_KEYS", *intake.GITLAB_TOKENS)}
        # The image copies source_credentials.py beside intake.py; here it stays in packages/
        path = os.pathsep.join(p for p in (str(REPO / "packages" / "source-credentials"), env.get("PYTHONPATH")) if p)
        return {**env, "DATABASE_URL": database_url(), "PYTHONPATH": path, **extra}

    def test_refuses_to_start_without_a_usable_webhook_secret(self):
        for secret, message in ((None, "is required"), ("  ", "is required"), ("secret\n", "has leading or trailing")):
            with self.subTest(repr(secret)):
                env = self.env() if secret is None else self.env(GITHUB_WEBHOOK_SECRET=secret)
                result = subprocess.run([sys.executable, str(HERE / "intake.py")], env=env, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"GITHUB_WEBHOOK_SECRET {message}", result.stderr)

    def test_refuses_to_start_with_an_unusable_slack_jira_or_gitlab_secret(self):
        for name in ("SLACK_SIGNING_SECRET", *intake.JIRA_SECRETS, *intake.GITLAB_TOKENS):
            for secret, message in (("  ", "is required"), ("secret\n", "has leading or trailing")):
                with self.subTest(name, secret=repr(secret)):
                    result = subprocess.run([sys.executable, str(HERE / "intake.py")],
                                            env=self.env(GITHUB_WEBHOOK_SECRET=SECRET, **{name: secret}),
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 2)
                    either = {tokens: f"{tokens[0]} or {tokens[1]} is required"
                              for tokens in (intake.JIRA_SECRETS, intake.GITLAB_TOKENS)}
                    pair = next((tokens for tokens in either if name in tokens), None)
                    self.assertIn(either[pair] if pair and message == "is required" else f"{name} {message}",
                                  result.stderr)
        # A signing token is whsec_ and base64, as GitLab shows it
        for token, message in (("not-whsec", "must start with whsec_"), ("whsec_!!", "is not base64")):
            with self.subTest(token):
                result = subprocess.run([sys.executable, str(HERE / "intake.py")],
                                        env=self.env(GITHUB_WEBHOOK_SECRET=SECRET, GITLAB_WEBHOOK_SIGNING_TOKEN=token),
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"GITLAB_WEBHOOK_SIGNING_TOKEN {message}", result.stderr)

    def test_refuses_to_start_with_an_unreadable_webhook_key_ring(self):
        for ring in ("no-colon", "test:not base64!", "test:" + base64.b64encode(b"short").decode()):
            with self.subTest(ring):
                result = subprocess.run([sys.executable, str(HERE / "intake.py")],
                                        env=self.env(GITHUB_WEBHOOK_SECRET=SECRET, SOURCE_CREDENTIALS_WEBHOOK_KEYS=ring),
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn("SOURCE_CREDENTIALS_WEBHOOK_KEYS can't be read", result.stderr)
                self.assertNotIn("not base64!", result.stderr.replace("'test'", ""))

    async def test_gitlab_is_served_only_with_a_token(self):
        signed = gitlab(GITLAB_FIXTURE, "process-signed")
        legacy = (gitlab(GITLAB_FIXTURE)[0], {"Idempotency-Key": "process-legacy", "X-Gitlab-Token": GITLAB_SECRET_TOKEN,
                                               "Content-Type": "application/json"})
        cases = (({}, "github", signed, 404),
                 ({"GITLAB_WEBHOOK_SIGNING_TOKEN": GITLAB_SIGNING_TOKEN}, "github, gitlab", signed, 200),
                 ({"GITLAB_WEBHOOK_SECRET_TOKEN": GITLAB_SECRET_TOKEN}, "github, gitlab", legacy, 200),
                 # EMBER-149: the key ring alone serves GitLab, and an account with no stored token gets a 401
                 ({"SOURCE_CREDENTIALS_WEBHOOK_KEYS": WEBHOOK_KEYS}, "github, gitlab", signed, 401))
        for extra, sources, (body, headers), status in cases:
            with self.subTest(extra=sorted(extra)):
                port = free_port()
                process = subprocess.Popen(
                    [sys.executable, str(HERE / "intake.py")],
                    env=self.env(GITHUB_WEBHOOK_SECRET=SECRET, LISTEN_PORT=str(port), **extra),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    for _ in range(300):
                        try:
                            if request(f"http://127.0.0.1:{port}/health")[0] == 200:
                                break
                        except OSError:
                            pass
                        await asyncio.sleep(0.05)
                    reply = await asyncio.to_thread(request, f"http://127.0.0.1:{port}/webhook/gitlab", body, headers)
                    self.assertEqual(reply[0], status, reply[1])
                finally:
                    process.send_signal(signal.SIGINT)
                    output = (await asyncio.to_thread(process.communicate, timeout=30))[0]
                self.assertIn(f"serving /webhook/{{source}} for {sources}", output)

    async def test_slack_and_jira_are_served_only_with_their_secrets(self):
        # Without them intake still runs for GitHub, so a rollout before an app or webhook is set up is safe
        cases = (({}, "github", 404, 404),
                 ({"SLACK_SIGNING_SECRET": SLACK_SECRET}, "github, slack", 200, 404),
                 ({"SLACK_SIGNING_SECRET": SLACK_SECRET, "JIRA_WEBHOOK_SECRET": JIRA_SECRET}, "github, slack, jira",
                  200, 200),
                 # Dynamic webhooks only: an admin webhook's signature is then refused
                 ({"JIRA_OAUTH_CLIENT_SECRET": JIRA_OAUTH_SECRET}, "github, jira", 404, 401))
        for extra, sources, slack_status, jira_status in cases:
            with self.subTest(sources):
                port = free_port()
                process = subprocess.Popen(
                    [sys.executable, str(HERE / "intake.py")],
                    env=self.env(GITHUB_WEBHOOK_SECRET=SECRET, LISTEN_PORT=str(port), **extra),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    for _ in range(300):
                        try:
                            if request(f"http://127.0.0.1:{port}/health")[0] == 200:
                                break
                        except OSError:
                            pass
                        await asyncio.sleep(0.05)
                    check = {"type": "url_verification", "challenge": "c"}
                    reply = await asyncio.to_thread(request, f"http://127.0.0.1:{port}/webhook/slack", *slack(check))
                    self.assertEqual(reply[0], slack_status)
                    reply = await asyncio.to_thread(request, f"http://127.0.0.1:{port}/webhook/jira",
                                                    *jira(JIRA_FIXTURE, f"process-{port}"))
                    self.assertEqual(reply[0], jira_status)
                finally:
                    process.send_signal(signal.SIGINT)
                    output = (await asyncio.to_thread(process.communicate, timeout=30))[0]
                self.assertIn(f"serving /webhook/{{source}} for {sources}", output)

    async def test_signal_finishes_the_request_in_flight_then_exits(self):
        # uvicorn re-raises SIGTERM once it has drained (as PID 1 in a container that exits 0 instead),
        # and uvicorn.run returns after Ctrl+C
        for sig, exit_code in ((signal.SIGTERM, -signal.SIGTERM), (signal.SIGINT, 0)):
            with self.subTest(sig.name):
                port = free_port()
                process = subprocess.Popen(
                    [sys.executable, str(HERE / "intake.py")], env=self.env(GITHUB_WEBHOOK_SECRET=SECRET, LISTEN_PORT=str(port)),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    for _ in range(300):
                        try:
                            if request(f"http://127.0.0.1:{port}/health")[0] == 200:
                                break
                        except OSError:
                            pass
                        await asyncio.sleep(0.05)
                    body, headers = github("issues", FIXTURE, f"in-flight-{sig.name}")
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                    writer.write(f"POST /webhook/github HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n{head}\r\n".encode()
                                 + body[:10])
                    await writer.drain()
                    await asyncio.sleep(0.3)
                    process.send_signal(sig)
                    await asyncio.sleep(0.5)
                    self.assertIsNone(process.poll(), "exited with a request in flight")
                    writer.write(body[10:])
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), 10)
                    writer.close()
                    self.assertTrue(response.startswith(b"HTTP/1.1 200"), response)
                    self.assertEqual(await asyncio.to_thread(process.wait, 10), exit_code)
                finally:
                    process.kill()
                    output = process.communicate()[0]
                self.assertNotIn("Traceback", output)
        db = await asyncpg.connect(database_url())
        try:
            self.assertEqual(await db.fetchval("select count(*) from raw_events where delivery_id like 'in-flight-%'"), 2)
        finally:
            await db.close()


if __name__ == "__main__":
    unittest.main()
