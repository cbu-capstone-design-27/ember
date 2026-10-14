"""intake against a real Postgres (EMBER-62). Nothing here calls GitHub.

Needs psql, dbmate, the packages in requirements.txt, and a Postgres server it may create and drop
a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/intake/test_intake.py

It builds the repo migrations in database ember_test_intake.
"""

from __future__ import annotations

import asyncio
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

import intake  # noqa: E402
from adapters import Delivery, Unauthorized  # noqa: E402
from adapters.github import RECOMMENDED_WEBHOOK_EVENTS, GitHubAdapter  # noqa: E402
from models import Installation, RawEvent  # noqa: E402
from validate import assert_valid  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_intake"
SECRET = "test-webhook-secret"
FIXTURE = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "github.json").read_text())["body"]


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
        return delivery.json["changeType"]

    def occurred_at(self, delivery: Delivery) -> None:
        return None


class Server:
    """create_app served by uvicorn on a free port, inside the test's event loop."""

    def __init__(self, url: str, registry: CollectorRegistry | None = None) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        app = intake.create_app(intake.create_engine(url), {"github": GitHubAdapter(SECRET), "teams": FakeTeams()},
                                registry)
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
        await self.db.execute("insert into installations (source, account_id, tenant) values ('github', '77', 'acme')")
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

    async def test_models_match_the_migrated_schema(self):
        for model in (RawEvent, Installation):
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
        self.assertEqual(response, b"HTTP/1.1 413 Request Entity Too Large\r\n")
        self.assertEqual([r["delivery_id"] for r in await self.rows()], ["at-limit"])

    async def test_postgres_unreachable_answers_503(self):
        down = urlunsplit(urlsplit(database_url())._replace(netloc=f"postgres@127.0.0.1:{free_port()}"))
        async with Server(down) as server:
            body, headers = github("issues", FIXTURE, "while-down")
            started = time.monotonic()
            self.assertEqual(await server.post("/webhook/github", body, headers),
                             (503, '{"error":"postgres unavailable"}'))
            self.assertLess(time.monotonic() - started, intake.DB_TIMEOUT)
            self.assertEqual((await server.get("/health"))[0], 503)
        self.assertEqual((await self.intake.get("/health")), (200, '{"status":"ok"}'))

    async def test_metrics_count_each_result(self):
        payload = {**FIXTURE, "installation": {"id": 77}}
        await self.deliver("issues", payload, "m-1")
        await self.deliver("issues", payload, "m-1")
        await self.deliver("issues", payload, "m-2", secret="other")
        await self.intake.post("/webhook/no-such-source", b"{}", {})
        await self.intake.post("/webhook/teams?validationToken=t", b"", {})

        status, text = await self.intake.get("/metrics")
        self.assertEqual(status, 200)
        counts = {(s.labels["source"], s.labels["result"]): s.value
                  for family in text_string_to_metric_families(text) if family.name == "intake_deliveries"
                  for s in family.samples if s.name == "intake_deliveries_total"}
        self.assertEqual(counts, {("github", "stored"): 1, ("github", "duplicate"): 1, ("github", "unauthorized"): 1,
                                  ("unknown", "unknown_source"): 1, ("teams", "handshake"): 1})

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


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    """`python3 intake.py` as the container runs it."""

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_WEBHOOK_SECRET", "DATABASE_URL")}
        return {**env, "DATABASE_URL": database_url(), **extra}

    def test_refuses_to_start_without_a_webhook_secret(self):
        result = subprocess.run([sys.executable, str(HERE / "intake.py")], env=self.env(), capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("GITHUB_WEBHOOK_SECRET is required", result.stderr)

    async def test_signal_finishes_the_request_in_flight_then_exits(self):
        # uvicorn re-raises SIGTERM once it has drained, and uvicorn.run returns after Ctrl+C
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
