"""enrich:slack (EMBER-67, rebuilt on the backend in EMBER-160) against a real Postgres. Slack is stubbed
at the exchange.

Needs psql, dbmate, this package installed (`pip install services/backend`), and a Postgres server it
may create and drop a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \\
        python3 services/backend/tests/test_enrich_slack.py

It builds the repo migrations in database ember_test_enrich_slack.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "packages" / "source-credentials"))

from ember.connectors.slack.backfill import SlackBackfill, Stopped  # noqa: E402
from ember.connectors.slack.enrich import SlackEnrich  # noqa: E402
from ember.core.credentials import Credentials  # noqa: E402
from ember.core.db import Database  # noqa: E402
from ember.core.errors import ConfigError  # noqa: E402
from ember.core.http import Response  # noqa: E402
from source_credentials import KeyRing  # noqa: E402

MIGRATIONS = REPO / "services" / "backend" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_enrich_slack"
TEAM = "T0PHOTON"
TENANT = "photon"
BOT = "UBOT"
TOKEN = "xoxb-photon"
KEY = bytes(range(32))
RING = KeyRing("api", [("test", KEY)])
ENV = {"SOURCE_CREDENTIALS_API_KEYS": "test:" + base64.b64encode(KEY).decode()}
CHANNEL = {"id": "C1", "name": "general", "is_private": False, "created": 1717000000, "updated": 1717000000000,
           "topic": {"value": "", "creator": "", "last_set": 0}}
ALICE = {"id": "U1", "name": "alice", "updated": 1717000000, "profile": {"real_name": "Alice"}}
T0 = 1717000100


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


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


def callback(event: dict, event_id: str, event_time: int = T0) -> dict:
    return {"type": "event_callback", "team_id": TEAM, "api_app_id": "A1", "event_id": event_id,
            "event_time": event_time, "event": event,
            "authorizations": [{"team_id": TEAM, "user_id": BOT, "is_bot": True}]}


def post(ts: str, text: str = "hi", user: str = "U1", **extra) -> dict:
    return {"type": "message", "channel": "C1", "channel_type": "channel", "user": user, "text": text,
            "ts": ts, "event_ts": ts, **extra}


class Slack:
    """The Web API for one bot token: users U1, channel C1, and the thread parents in `messages`. `answers`
    holds canned responses by method, used first."""

    def __init__(self, **answers: list[Response]) -> None:
        self.answers = {method.replace("_", "."): list(responses) for method, responses in answers.items()}
        self.messages: dict[str, dict] = {}   # thread parents conversations.replies finds, by ts
        self.channels: list[dict] = []          # the bot's channels (users.conversations)
        self.history: dict[str, list[dict]] = {}  # channel id: its messages, newest first
        self.replies: dict[str, list[dict]] = {}  # thread ts: parent then replies
        self.calls: list[str] = []
        self.params: list[dict] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        parts = urlsplit(url)
        name, query = parts.path.removeprefix("/api/"), {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append(name)
        self.params.append(query)
        if self.answers.get(name):
            return self.answers[name].pop(0)
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.ok(False, error="invalid_auth")
        if name == "users.info":
            return self.ok(user=ALICE) if query["user"] == "U1" else self.ok(False, error="user_not_found")
        if name == "conversations.info":
            return self.ok(channel=CHANNEL) if query["channel"] == "C1" else self.ok(False, error="channel_not_found")
        if name == "users.conversations":
            return self.page(self.channels, query, "channels")
        if name == "conversations.history":
            newer = [m for m in self.history.get(query["channel"], []) if float(m["ts"]) > float(query["oldest"])]
            return self.page(newer, query, "messages")
        if name == "conversations.replies" and query["ts"] in self.replies:
            return self.page(self.replies[query["ts"]], query, "messages")
        if name == "conversations.replies":
            found = self.messages.get(query["ts"])
            return self.ok(messages=[found]) if found else self.ok(False, error="thread_not_found")
        return Response(404, {}, b"")

    def page(self, items: list, query: dict, field: str, size: int = 2) -> Response:
        """Two items a page, with Slack's cursor paging."""
        start = int(query.get("cursor") or 0)
        rest = start + size < len(items)
        return self.ok(**{field: items[start:start + size]},
                       response_metadata={"next_cursor": str(start + size) if rest else ""})

    @staticmethod
    def ok(ok: bool = True, **fields) -> Response:
        return Response(200, {}, json.dumps({"ok": ok, **fields}).encode())


class SlackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url())
        await self.db.execute("truncate raw_events, source_records, rate_limits, cursors, installations, source_credentials"
                              " restart identity cascade")
        await self.connect()
        self.running: list[tuple[asyncio.Event, asyncio.Task]] = []
        self.credentials = Credentials(Database(database_url()), ENV)

    async def asyncTearDown(self):
        for stopping, task in self.running:
            stopping.set()
            await asyncio.wait_for(task, 10)
        await self.credentials.close()
        await self.db.close()

    async def connect(self, tenant: str = TENANT, token: str = TOKEN, bot: str = BOT) -> None:
        """What Connect Slack writes (EMBER-148): the installations row and the sealed bot token."""
        sealed = RING.seal("slack", TEAM, {"bot_token": token, "bot_user_id": bot, "scope": "channels:history"})
        await self.db.execute("insert into installations (source, account_id, tenant) values ('slack', $1, $2)"
                              " on conflict (source, account_id) do update"
                              " set tenant = excluded.tenant, disconnected_at = null", TEAM, tenant)
        await self.db.execute("insert into source_credentials (source, account_id, kind, key_id, ciphertext)"
                              " values ('slack', $1, 'api', $2, $3) on conflict (source, account_id, kind) do update"
                              " set key_id = excluded.key_id, ciphertext = excluded.ciphertext, updated_at = now()",
                              TEAM, sealed.key_id, sealed.ciphertext)

    async def insert(self, body: dict, occurred: float, tenant: str = TENANT) -> int:
        event = body["event"]
        name = event["type"] + (f":{event['subtype']}" if event.get("subtype") else "")
        return await self.db.fetchval(
            "insert into raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope, occurred_at)"
            " values ('slack', $1, $2, $3, $4, 'webhook', $5::jsonb, $6) returning id",
            body["event_id"], TEAM, tenant, name, json.dumps({"type": "slack", "body": body}), at(occurred))

    def start(self, slack: Slack) -> SlackEnrich:
        worker = SlackEnrich(Database(database_url()), self.credentials, exchange=slack, poll_seconds=60)
        stopping = asyncio.Event()

        async def go():
            await worker.start()
            try:
                await worker.run(stopping)
            finally:
                await worker.close()
        self.running.append((stopping, asyncio.create_task(go())))
        return worker

    async def wait_for(self, sql: str, *args, timeout: float = 5.0) -> None:
        start = time.monotonic()
        while not await self.db.fetchval(sql, *args):
            if time.monotonic() - start > timeout:
                raise AssertionError(f"timed out waiting for: {sql}")
            await asyncio.sleep(0.02)

    async def enrich(self, slack: Slack | None = None) -> Slack:
        slack = slack or Slack()
        self.start(slack)
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")
        return slack

    async def records(self, kind: str | None = None) -> list[tuple]:
        rows = await self.db.fetch("select kind, object_id, version, occurred_at, record from source_records"
                                   " where $1::text is null or kind = $1 order by occurred_at, id", kind)
        return [(r["kind"], r["object_id"], r["version"], r["occurred_at"], json.loads(r["record"])) for r in rows]


class EnrichTest(SlackTest):
    async def test_a_message_fetches_its_author_and_channel_once(self):
        await self.insert(callback(post("1717000100.000100"), "Ev1"), 1717000100.0001)
        await self.insert(callback(post("1717000101.000200", "again"), "Ev2"), 1717000101.0002)
        slack = await self.enrich()
        self.assertEqual(sorted(slack.calls), ["conversations.info", "users.info"])
        kinds = [(kind, object_id, version) for kind, object_id, version, _, _ in await self.records()]
        self.assertIn(("user", "U1", "1717000000"), kinds)
        self.assertIn(("channel", "C1", "1717000000000"), kinds)
        messages = await self.records("message")
        self.assertEqual([m[1:3] for m in messages], [("C1/1717000100.000100", "1717000100.000100"),
                                                      ("C1/1717000101.000200", "1717000101.000200")])
        self.assertEqual(messages[0][3], at(1717000100.0001))  # occurred_at is the row's, exact to the microsecond
        record = messages[1][4]
        self.assertEqual((record["user"]["name"], record["channel"]["name"], record["deleted"]), ("alice", "general", False))
        self.assertEqual(record["payload"]["event_id"], "Ev2")

    async def test_a_reply_fetches_a_missing_parent_and_the_parents_delivery_replaces_that_copy(self):
        slack = Slack()
        slack.messages["1717000100.000100"] = {"type": "message", "user": "U1", "text": "parent",
                                               "ts": "1717000100.000100", "thread_ts": "1717000100.000100"}
        await self.insert(callback(post("1717000105.000100", "reply", thread_ts="1717000100.000100"), "Ev2"), 1717000105)
        await self.enrich(slack)
        self.assertIn("conversations.replies", slack.calls)
        reply = (await self.records("message"))[-1][4]
        self.assertEqual(reply["thread_parent"]["text"], "parent")
        fetched = [m for m in await self.records("message") if m[1] == "C1/1717000100.000100"]
        self.assertIsNone(fetched[0][4]["payload"])

        await self.insert(callback(post("1717000100.000100", "parent", thread_ts="1717000100.000100"), "Ev1"), 1717000100)
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")
        parent = [m for m in await self.records("message") if m[1] == "C1/1717000100.000100"]
        self.assertEqual(len(parent), 1)
        self.assertEqual(parent[0][4]["payload"]["event_id"], "Ev1")

    async def test_edits_and_deletes_are_versions_and_a_tombstone(self):
        edit = {"type": "message", "subtype": "message_changed", "channel": "C1", "ts": "1717000200.000001",
                "message": {"type": "message", "user": "U1", "text": "edited", "ts": "1717000100.000100",
                            "edited": {"user": "U1", "ts": "1717000200.000000"}}}
        gone = {"type": "message", "subtype": "message_deleted", "channel": "C1", "ts": "1717000300.000001",
                "deleted_ts": "1717000100.000100", "previous_message": {"user": "U1", "text": "edited"}}
        await self.insert(callback(post("1717000100.000100"), "Ev1"), 1717000100)
        await self.insert(callback(edit, "Ev2"), 1717000200)
        await self.insert(callback(gone, "Ev3"), 1717000300)
        await self.enrich()
        rows = [(m[1], m[2], m[4]["deleted"]) for m in await self.records("message")]
        self.assertEqual(rows, [("C1/1717000100.000100", "1717000100.000100", False),
                                ("C1/1717000100.000100", "1717000200.000000", False),
                                ("C1/1717000100.000100", "deleted:1717000300.000001", True)])

    async def test_channel_changes_apply_onto_the_channel_stored_before_them(self):
        rename = {"type": "channel_rename", "channel": {"id": "C1", "name": "news", "created": 1717000000}}
        topic = post("1717000300.000100", subtype="channel_topic", topic="Launch")
        archive = {"type": "channel_archive", "channel": "C1", "user": "U1"}
        await self.insert(callback(rename, "Ev1", 1717000200), 1717000200)
        await self.insert(callback(topic, "Ev2"), 1717000300.0001)
        await self.enrich()
        channels = await self.records("channel")
        self.assertEqual([c[2] for c in channels], ["1717000200:Ev1", "1717000300.000100"])
        self.assertEqual(channels[0][4]["is_private"], False)  # fetched before the first change, then changed
        self.assertEqual((channels[-1][4]["name"], channels[-1][4]["topic"]["value"]), ("news", "Launch"))
        self.assertEqual((await self.records("message"))[0][4]["channel"]["topic"]["value"], "Launch")

        # A late archive lands at its own time, onto the rename, and leaves the newer row current
        await self.insert(callback(archive, "Ev3", 1717000250), 1717000250)
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")
        channels = await self.records("channel")
        late = [c for c in channels if c[2] == "1717000250:Ev3"][0][4]
        self.assertEqual((late["name"], late["is_archived"]), ("news", True))
        self.assertEqual(channels[-1][2], "1717000300.000100")

    async def test_membership_and_user_events(self):
        joined = {"type": "member_joined_channel", "user": "U1", "channel": "C1", "channel_type": "C", "inviter": "U9"}
        left = {"type": "member_left_channel", "user": "U2", "channel": "C1", "channel_type": "C"}
        change = {"type": "user_change", "user": {**ALICE, "updated": 1717000500, "name": "alice2"},
                  "event_ts": "1717000500.000100"}
        await self.insert(callback(joined, "Ev1"), T0)
        await self.insert(callback(left, "Ev2"), T0 + 1)
        await self.insert(callback(change, "Ev3"), 1717000500.0001)
        slack = await self.enrich()
        self.assertEqual(slack.calls, ["users.info"])  # the join's person; a leave fetches nothing
        members = await self.records("membership")
        self.assertEqual([(m[1], m[2], m[4]["member"]) for m in members],
                         [("C1/U1", f"{T0}:Ev1", True), ("C1/U2", f"{T0}:Ev2", False)])
        self.assertEqual(members[0][4]["inviter"], "U9")
        self.assertEqual([(u[2], u[4]["name"]) for u in await self.records("user")],
                         [("1717000000", "alice"), ("1717000500", "alice2")])

    async def test_a_lookup_that_finds_nothing_still_writes_the_record(self):
        await self.insert(callback(post("1717000100.000100", user="U404"), "Ev1"), T0)
        await self.enrich()
        self.assertIsNone((await self.records("message"))[0][4]["user"])

    async def test_rows_wait_until_their_workspace_is_connected_to_their_tenant(self):
        await self.db.execute("update installations set tenant = 'other'")
        await self.insert(callback(post("1717000100.000100"), "Ev1"), T0)
        slack = Slack()
        self.start(slack)
        await asyncio.sleep(0.5)
        self.assertEqual(slack.calls, [])
        self.assertEqual(await self.db.fetchval("select count(*) from raw_events where enriched_at is null"), 1)
        await self.connect()
        await self.db.execute("notify raw_events, 'slack'")  # what the 30 s poll does, sooner
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")

    async def test_a_refused_token_disconnects_the_workspace_and_its_rows_wait(self):
        await self.connect(token="xoxb-revoked")
        await self.insert(callback(post("1717000100.000100"), "Ev1"), T0)
        slack = Slack()
        self.start(slack)
        await self.wait_for("select disconnected_at is not null from installations where account_id = $1", TEAM)
        self.assertEqual(await self.db.fetchval("select count(*) from source_credentials"), 0)
        self.assertEqual(await self.db.fetchval("select count(*) from source_records"), 0)
        await self.connect()
        await self.db.execute("notify raw_events, 'slack'")
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")

    async def test_uninstall_disconnects_unless_reconnected_since(self):
        await self.db.execute("update source_credentials set updated_at = now() - interval '1 hour'")
        await self.insert(callback({"type": "app_uninstalled"}, "Ev1"), time.time() - 60)
        await self.enrich()
        self.assertIsNotNone(await self.db.fetchval("select disconnected_at from installations"))

        await self.connect()  # reconnected after the uninstall: a retried delivery of it changes nothing
        await self.db.execute("update raw_events set enriched_at = null")
        await self.db.execute("notify raw_events, 'slack'")
        await self.wait_for("select count(*) = 0 from raw_events where enriched_at is null")
        self.assertIsNone(await self.db.fetchval("select disconnected_at from installations"))

    async def test_tokens_revoked_disconnects_only_for_our_bot(self):
        await self.insert(callback({"type": "tokens_revoked", "tokens": {"oauth": ["U1"], "bot": ["UOTHER"]}}, "Ev1"),
                          time.time() + 60)
        await self.enrich()
        self.assertIsNone(await self.db.fetchval("select disconnected_at from installations"))
        await self.insert(callback({"type": "tokens_revoked", "tokens": {"bot": [BOT]}}, "Ev2"), time.time() + 60)
        await self.wait_for("select disconnected_at is not null from installations")

    async def test_a_rate_limit_sets_the_row_aside_and_holds_the_workspace_method(self):
        slack = Slack(users_info=[Response(429, {"Retry-After": "120"}, b'{"ok": false, "error": "ratelimited"}')])
        await self.insert(callback(post("1717000100.000100"), "Ev1"), T0)
        worker = self.start(slack)
        await self.wait_for("select count(*) = 1 from rate_limits where bucket = $1", f"{TEAM}/users.info")
        wait = await self.db.fetchval("select extract(epoch from next_allowed_at - now()) from rate_limits"
                                      " where bucket = $1", f"{TEAM}/users.info")
        self.assertGreater(wait, 100)
        await self.wait_for("select count(*) = 0 from source_records")
        await asyncio.sleep(0.2)
        self.assertEqual(worker.status()["retrying"], 1)

    async def test_without_the_api_ring_it_does_not_start(self):
        credentials = Credentials(Database(database_url()), {})
        worker = SlackEnrich(Database(database_url()), credentials)
        try:
            with self.assertRaisesRegex(ConfigError, "SOURCE_CREDENTIALS_API_KEYS is not set"):
                await worker.start()
        finally:
            await worker.close()
            await credentials.close()



class BackfillTest(SlackTest):
    def workspace(self) -> Slack:
        slack = Slack()
        slack.channels = [{"id": "C1", "name": "general"}, {"id": "C2", "name": "random"}, {"id": "C3", "name": "ops"}]
        slack.history["C1"] = [
            {"type": "message", "user": "U1", "text": "edited", "ts": "1717000300.000100",
             "edited": {"user": "U1", "ts": "1717000400.000000"}},
            {"type": "message", "user": "U1", "text": "parent", "ts": "1717000200.000100",
             "thread_ts": "1717000200.000100", "reply_count": 2},
            {"type": "message", "user": "U1", "text": "first", "ts": "1717000100.000100"},
        ]
        slack.replies["1717000200.000100"] = [
            slack.history["C1"][1],
            {"type": "message", "user": "U1", "text": "r1", "ts": "1717000210.000100", "thread_ts": "1717000200.000100"},
            {"type": "message", "user": "U1", "text": "r2", "ts": "1717000220.000100", "thread_ts": "1717000200.000100"},
        ]
        slack.history["C3"] = [{"type": "message", "user": "U1", "text": "ops", "ts": "1717000500.000100"}]
        return slack

    async def backfill(self, slack: Slack, stopping: asyncio.Event | None = None) -> SlackBackfill:
        component = SlackBackfill(Database(database_url()), self.credentials, exchange=slack)
        await component.start()
        try:
            await component.run_once(stopping or asyncio.Event())
        except Stopped:  # run() ends on it
            pass
        finally:
            await component.close()
        return component

    async def test_a_connected_workspace_is_backfilled_and_enriched_like_webhooks(self):
        slack = self.workspace()
        component = await self.backfill(slack)
        rows = await self.db.fetch("select delivery_id, event_name, origin, occurred_at from raw_events order by id")
        self.assertEqual({r["delivery_id"] for r in rows}, {
            "C1/1717000300.000100@1717000400.000000", "C1/1717000200.000100@1717000200.000100",
            "C1/1717000100.000100@1717000100.000100", "C1/1717000210.000100@1717000210.000100",
            "C1/1717000220.000100@1717000220.000100", "C3/1717000500.000100@1717000500.000100"})
        self.assertEqual({r["origin"] for r in rows}, {"reconcile"})
        edited = [r for r in rows if r["delivery_id"].startswith("C1/1717000300")][0]
        self.assertEqual((edited["event_name"], edited["occurred_at"]), ("message:message_changed", at(1717000400)))
        self.assertEqual(component.last["rows"], 6)
        self.assertEqual(await self.db.fetchval("select position from cursors where stream = 'channel:C1'"),
                         "1717000300.000100")

        # A webhook copy of one of them is no new record
        await self.insert(callback(post("1717000100.000100", "first"), "Ev1"), 1717000100.0001)
        await self.enrich()
        messages = await self.records("message")
        self.assertEqual(sorted((m[1], m[2]) for m in messages), [
            ("C1/1717000100.000100", "1717000100.000100"), ("C1/1717000200.000100", "1717000200.000100"),
            ("C1/1717000210.000100", "1717000210.000100"), ("C1/1717000220.000100", "1717000220.000100"),
            ("C1/1717000300.000100", "1717000400.000000"), ("C3/1717000500.000100", "1717000500.000100")])
        reply = [m for m in messages if m[1] == "C1/1717000210.000100"][0][4]
        self.assertEqual(reply["thread_parent"]["text"], "parent")

    async def test_the_next_pass_reads_only_what_is_new_and_waits_its_turn(self):
        slack = self.workspace()
        await self.backfill(slack)
        slack.calls.clear()
        slack.params.clear()
        await self.backfill(slack)  # read within the hour: not due
        self.assertEqual(slack.calls, [])

        await self.db.execute("update cursors set updated_at = now() - interval '2 hours' where stream like 'workspace:%'")
        slack.history["C1"].insert(0, {"type": "message", "user": "U1", "text": "new", "ts": "1717000600.000100"})
        component = await self.backfill(slack)
        self.assertEqual(component.last["rows"], 1)
        oldest = {p["channel"]: p["oldest"] for name, p in zip(slack.calls, slack.params) if name == "conversations.history"}
        self.assertEqual(oldest["C1"], "1717000300.000100")

    async def test_a_rate_limit_is_waited_out(self):
        slack = self.workspace()
        slack.answers["conversations.history"] = [Response(429, {"Retry-After": "1"}, b'{"ok": false}')]
        started = time.monotonic()
        component = await self.backfill(slack)
        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.assertEqual(component.last["rows"], 6)

    async def test_a_refused_token_disconnects_the_workspace(self):
        await self.connect(token="xoxb-revoked")
        await self.backfill(self.workspace())
        self.assertIsNotNone(await self.db.fetchval("select disconnected_at from installations"))
        self.assertEqual(await self.db.fetchval("select count(*) from cursors"), 0)

    async def test_stopping_leaves_the_channel_to_the_next_pass(self):
        stopping = asyncio.Event()
        stopping.set()
        await self.backfill(self.workspace(), stopping)
        self.assertEqual(await self.db.fetchval("select count(*) from cursors"), 0)


if __name__ == "__main__":
    unittest.main()
