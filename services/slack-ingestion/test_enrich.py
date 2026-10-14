"""enrich:slack without a database: what each delivery becomes, and what is fetched for it (EMBER-67).

The cases follow docs/ingestion/slack-webhooks.md (Messages). No test here calls Slack: FakeSlackApi
answers users.info, conversations.info and conversations.replies from memory. test_enrich_db.py
runs the same worker against Postgres.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import enrich  # noqa: E402
from enrich import ConfigError, Permanent, database_urls, describe, enrich_delivery, run_async, slack_time  # noqa: E402
from pull_test import MethodGates, Response, make_client  # noqa: E402

TOKEN = "xoxb-test-token"
TEAM = "T0TEAM"
CHANNEL = "C0DEC"

USERS = {
    "U0ADA": {"id": "U0ADA", "name": "ada", "updated": 1700000000, "profile": {"email": "ada@widget.test"}},
    "U0BEN": {"id": "U0BEN", "name": "ben", "updated": 1700000001, "profile": {"email": "ben@widget.test"}},
}
CHANNELS = {
    CHANNEL: {"id": CHANNEL, "name": "decisions", "is_private": False, "created": 1690000000, "updated": 1700000000123},
}
PARENT = {"type": "message", "user": "U0ADA", "text": "Decision: use Neo4j.", "ts": "1700000100.000100", "thread_ts": "1700000100.000100", "reply_count": 1}
THREADS = {(CHANNEL, PARENT["ts"]): [PARENT]}


def _json(payload, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, headers or {}, json.dumps(payload).encode("utf-8"))


class FakeSlackApi:
    """Answers the Web API lookups enrich makes.

    errors maps a method to an ok:false error. throttle maps a method to how many 429s it answers
    first. delay maps a method to seconds it takes. garbage lists methods that answer with a body
    that is not JSON. started[method] is set once that method has been called.
    """

    def __init__(self, *, users=None, channels=None, threads=None, errors=None, throttle=None, delay=None, garbage=()):
        self.users = dict(USERS if users is None else users)
        self.channels = dict(CHANNELS if channels is None else channels)
        self.threads = dict(THREADS if threads is None else threads)
        self.errors = dict(errors or {})
        self.throttle = dict(throttle or {})
        self.delay = dict(delay or {})
        self.garbage = set(garbage)
        self.calls: list[tuple[str, dict]] = []
        self.started: dict[str, threading.Event] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def count(self, method: str) -> int:
        with self._lock:
            return sum(1 for name, _ in self.calls if name == method)

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        api_method = parts.path.rsplit("/", 1)[-1]
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert method == "GET" and body is None
        assert headers.get("Authorization") == f"Bearer {TOKEN}"
        with self._lock:
            self.calls.append((api_method, params))
            self.started.setdefault(api_method, threading.Event()).set()
            if self.throttle.get(api_method):
                self.throttle[api_method] -= 1
                return _json({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "1"})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay.get(api_method):
                time.sleep(self.delay[api_method])
            return self._answer(api_method, params)
        finally:
            with self._lock:
                self.in_flight -= 1

    def _answer(self, api_method: str, params: dict) -> Response:
        if api_method in self.garbage:
            return Response(200, {}, b"<html>not json</html>")
        if api_method in self.errors:
            return _json({"ok": False, "error": self.errors[api_method]})
        if api_method == "users.info":
            user = self.users.get(params.get("user"))
            return _json({"ok": True, "user": user} if user else {"ok": False, "error": "user_not_found"})
        if api_method == "conversations.info":
            channel = self.channels.get(params.get("channel"))
            return _json({"ok": True, "channel": channel} if channel else {"ok": False, "error": "channel_not_found"})
        if api_method == "conversations.replies":
            messages = self.threads.get((params.get("channel"), params.get("ts")))
            if messages is None:
                return _json({"ok": False, "error": "thread_not_found"})
            return _json({"ok": True, "messages": messages[: int(params.get("limit", 100))], "has_more": False})
        return _json({"ok": False, "error": "unknown_method"}, 404)


class DictCache:
    """The Cache, from a dict of (kind, object_id) -> record."""

    def __init__(self, records: dict | None = None):
        self.records = dict(records or {})

    async def latest(self, kind, object_id):
        return self.records.get((kind, object_id))


def caller(fake: FakeSlackApi):
    """A Call through the real SlackClient, with no pacing and no real sleeping."""
    client = make_client(TOKEN, fake, gates=MethodGates(time.monotonic, lambda _s: None))

    async def call(method, params):
        return client.call(method, params)

    return call


def callback(event: dict, *, event_id: str = "Ev1", event_time: int = 1700000200) -> dict:
    """An Events API delivery body around ``event``."""
    return {
        "type": "event_callback",
        "team_id": TEAM,
        "api_app_id": "A0APP",
        "event_id": event_id,
        "event_time": event_time,
        "event": event,
    }


def message(ts: str, *, user: str = "U0ADA", text: str = "hello", **extra) -> dict:
    return {"type": "message", "channel": CHANNEL, "user": user, "text": text, "ts": ts, "event_ts": ts, **extra}


def by_kind(records) -> dict[str, list]:
    out: dict[str, list] = {}
    for rec in records:
        out.setdefault(rec.kind, []).append(rec)
    return out


class DescribeTest(unittest.TestCase):
    """Each message shape's object_id, version and occurred_at (slack-webhooks.md, Messages)."""

    def test_new_message(self):
        d = describe(callback(message("1700000200.000100")))
        self.assertEqual((d.shape, d.object_id, d.version), ("new", f"{CHANNEL}/1700000200.000100", "1700000200.000100"))
        self.assertEqual(d.user, "U0ADA")
        self.assertIsNone(d.thread_ts)
        self.assertEqual(d.occurred_at, datetime(2023, 11, 14, 22, 16, 40, 100, tzinfo=timezone.utc))

    def test_the_shared_fixture_is_a_new_message(self):
        envelope = json.loads((REPO / "packages" / "ingestion-envelope" / "fixtures" / "slack.json").read_text())
        d = describe(envelope["body"])
        self.assertEqual((d.shape, d.object_id), ("new", "C012AB3CD/1717000001.000200"))

    def test_bot_file_and_system_messages_are_messages(self):
        for subtype in ("bot_message", "file_share", "me_message", "channel_join", "channel_topic"):
            with self.subTest(subtype=subtype):
                d = describe(callback(message("1700000200.000100", subtype=subtype)))
                self.assertEqual((d.shape, d.version), ("new", "1700000200.000100"))
        bot = message("1700000200.000100", subtype="bot_message", bot_id="B0BOT")
        del bot["user"]
        self.assertIsNone(describe(callback(bot)).user)

    def test_a_thread_parent_is_not_a_reply(self):
        d = describe(callback(message("1700000100.000100", thread_ts="1700000100.000100")))
        self.assertEqual(d.shape, "new")
        self.assertIsNone(d.thread_ts)

    def test_reply(self):
        d = describe(callback(message("1700000200.000100", thread_ts=PARENT["ts"])))
        self.assertEqual((d.shape, d.object_id, d.thread_ts), ("reply", f"{CHANNEL}/1700000200.000100", PARENT["ts"]))

    def test_thread_broadcast_carries_its_parent(self):
        d = describe(callback(message("1700000250.000100", subtype="thread_broadcast", thread_ts=PARENT["ts"], root=PARENT)))
        self.assertEqual((d.shape, d.thread_ts, d.parent), ("broadcast", PARENT["ts"], PARENT))

    def test_message_replied_with_and_without_its_subtype(self):
        nested = dict(PARENT, reply_count=2)
        with_subtype = {"type": "message", "subtype": "message_replied", "channel": CHANNEL, "hidden": True,
                        "message": nested, "ts": "1700000300.000200", "event_ts": "1700000300.000200"}
        without = {k: v for k, v in with_subtype.items() if k != "subtype"}
        for event in (with_subtype, without):
            d = describe(callback(event))
            self.assertEqual((d.shape, d.object_id, d.version), ("replied", f"{CHANNEL}/{PARENT['ts']}", "1700000300.000200"))

    def test_edit_is_a_new_version_of_the_original(self):
        event = {"type": "message", "subtype": "message_changed", "hidden": True, "channel": CHANNEL, "ts": "1700000400.000300",
                 "message": message("1700000200.000100", text="edited", edited={"user": "U0ADA", "ts": "1700000400.000000"})}
        d = describe(callback(event))
        self.assertEqual((d.shape, d.object_id, d.version), ("changed", f"{CHANNEL}/1700000200.000100", "1700000400.000000"))
        self.assertEqual(d.occurred_at, slack_time("1700000400.000300"))  # when it was edited
        self.assertEqual(d.message["text"], "edited")

    def test_edit_without_edited_uses_the_event_ts(self):
        event = {"type": "message", "subtype": "message_changed", "channel": CHANNEL, "ts": "1700000400.000300",
                 "message": message("1700000200.000100")}
        self.assertEqual(describe(callback(event)).version, "1700000400.000300")

    def test_edited_reply_keeps_its_parent(self):
        event = {"type": "message", "subtype": "message_changed", "channel": CHANNEL, "ts": "1700000400.000300",
                 "message": message("1700000200.000100", thread_ts=PARENT["ts"], edited={"ts": "1700000400.000000"})}
        self.assertEqual(describe(callback(event)).thread_ts, PARENT["ts"])

    def test_delete_is_a_tombstone(self):
        event = {"type": "message", "subtype": "message_deleted", "hidden": True, "channel": CHANNEL,
                 "ts": "1700000500.000400", "deleted_ts": "1700000200.000100",
                 "previous_message": message("1700000200.000100", user="U0BEN")}
        d = describe(callback(event))
        self.assertEqual((d.shape, d.object_id, d.version), ("deleted", f"{CHANNEL}/1700000200.000100", "deleted:1700000500.000400"))
        self.assertEqual(d.user, "U0BEN")
        bare = describe(callback({k: v for k, v in event.items() if k != "previous_message"}))
        self.assertIsNone(bare.message)
        self.assertIsNone(bare.user)

    def test_occurred_at_is_event_ts_then_ts_then_event_time(self):
        event = message("1700000200.000100", event_ts="1700000201.000000")
        self.assertEqual(describe(callback(event)).occurred_at, slack_time("1700000201.000000"))
        del event["event_ts"]
        self.assertEqual(describe(callback(event)).occurred_at, slack_time("1700000200.000100"))
        edit = {"type": "message", "subtype": "message_changed", "channel": CHANNEL,
                "message": message("1700000200.000100", edited={"ts": "1700000400.000000"})}
        self.assertEqual(describe(callback(edit, event_time=1700000999)).occurred_at, slack_time(1700000999))

    def test_a_message_with_an_unreadable_ts_is_permanent(self):
        with self.assertRaises(Permanent):
            describe(callback({"type": "message", "channel": CHANNEL, "user": "U0ADA", "ts": "x"}))

    def test_other_events_are_skipped(self):
        for body in (
            callback({"type": "reaction_added", "user": "U0ADA", "reaction": "white_check_mark",
                      "item": {"type": "message", "channel": CHANNEL, "ts": "1"}, "event_ts": "1700000600.000100"}),
            callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "new"}}),
            {"type": "app_rate_limited", "team_id": TEAM, "minute_rate_limited": 1700000000},
            {"type": "message", "ts": "1700000200.000100"},  # a backfill object, not a delivery
            None,
        ):
            with self.subTest(body=body):
                self.assertIsNone(describe(body))

    def test_message_events_missing_what_they_need_are_permanent(self):
        for event in (
            {"type": "message", "ts": "1700000200.000100"},  # no channel
            {"type": "message", "channel": CHANNEL, "event_ts": "1700000200.000100"},  # no ts
            {"type": "message", "subtype": "message_changed", "channel": CHANNEL, "ts": "1700000200.000100"},
            {"type": "message", "subtype": "message_deleted", "channel": CHANNEL, "ts": "1700000200.000100"},
        ):
            with self.subTest(event=event), self.assertRaises(Permanent):
                describe(callback(event))


class EnrichDeliveryTest(unittest.TestCase):
    """What is fetched, what is cached, and the records a delivery becomes."""

    def run_enrich(self, body, fake=None, cache=None):
        fake = fake or FakeSlackApi()
        records = run_async(enrich_delivery(body, cache=cache or DictCache(), call=caller(fake)))
        return records, fake

    def test_new_message_fetches_and_caches_its_user_and_channel(self):
        body = callback(message("1700000200.000100"))
        records, fake = self.run_enrich(body)
        kinds = by_kind(records)
        self.assertEqual(sorted(kinds), ["channel", "message", "user"])
        user, channel, msg = kinds["user"][0], kinds["channel"][0], kinds["message"][0]
        self.assertEqual((user.object_id, user.version), ("U0ADA", "1700000000"))
        self.assertEqual((channel.object_id, channel.version), (CHANNEL, "1700000000123"))
        self.assertEqual(channel.occurred_at, datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=timezone.utc))
        self.assertEqual(msg.record["payload"], body)
        self.assertEqual(msg.record["message"], body["event"])
        self.assertEqual(msg.record["user"]["profile"]["email"], "ada@widget.test")
        self.assertEqual(msg.record["channel"]["name"], "decisions")
        self.assertIsNone(msg.record["thread_parent"])
        self.assertFalse(msg.record["deleted"])
        self.assertEqual(records[-1], msg)  # the message comes after what was fetched for it
        self.assertEqual((fake.count("users.info"), fake.count("conversations.info"), fake.count("conversations.replies")), (1, 1, 0))

    def test_stored_user_and_channel_are_not_fetched_again(self):
        cache = DictCache({("user", "U0ADA"): USERS["U0ADA"], ("channel", CHANNEL): CHANNELS[CHANNEL]})
        records, fake = self.run_enrich(callback(message("1700000200.000100")), cache=cache)
        self.assertEqual([r.kind for r in records], ["message"])
        self.assertEqual(records[0].record["user"], USERS["U0ADA"])
        self.assertEqual(fake.calls, [])

    def test_reply_fetches_its_parent_once_and_stores_it(self):
        body = callback(message("1700000200.000100", user="U0BEN", thread_ts=PARENT["ts"]))
        records, fake = self.run_enrich(body)
        kinds = by_kind(records)
        parent = kinds["message"][0]
        self.assertEqual((parent.object_id, parent.version), (f"{CHANNEL}/{PARENT['ts']}", PARENT["ts"]))
        self.assertEqual(parent.record["message"]["channel"], CHANNEL)  # set, as the backfill does
        self.assertIsNone(parent.record["payload"])
        reply = records[-1]
        self.assertEqual(reply.object_id, f"{CHANNEL}/1700000200.000100")
        self.assertEqual(reply.record["thread_parent"]["text"], PARENT["text"])
        self.assertEqual(fake.calls[-1], ("conversations.replies", {"channel": CHANNEL, "ts": PARENT["ts"], "limit": "1"}))

    def test_reply_to_a_stored_parent_does_not_fetch_it(self):
        stored = enrich.message_record(None, PARENT, deleted=False)
        cache = DictCache({("message", f"{CHANNEL}/{PARENT['ts']}"): stored})
        records, fake = self.run_enrich(callback(message("1700000200.000100", thread_ts=PARENT["ts"])), cache=cache)
        self.assertEqual(records[-1].record["thread_parent"], PARENT)
        self.assertEqual(fake.count("conversations.replies"), 0)

    def test_broadcast_uses_the_parent_it_carries(self):
        body = callback(message("1700000250.000100", subtype="thread_broadcast", thread_ts=PARENT["ts"], root=PARENT))
        records, fake = self.run_enrich(body)
        self.assertEqual(records[-1].record["thread_parent"], PARENT)
        self.assertEqual(fake.count("conversations.replies"), 0)

    def test_edit_and_delete(self):
        edit = callback({"type": "message", "subtype": "message_changed", "channel": CHANNEL, "ts": "1700000400.000300",
                         "message": message("1700000200.000100", text="edited", edited={"ts": "1700000400.000000"})})
        records, _ = self.run_enrich(edit)
        self.assertEqual((records[-1].version, records[-1].record["message"]["text"]), ("1700000400.000000", "edited"))
        delete = callback({"type": "message", "subtype": "message_deleted", "channel": CHANNEL, "ts": "1700000500.000400",
                           "deleted_ts": "1700000200.000100"})
        records, fake = self.run_enrich(delete)
        tombstone = records[-1]
        self.assertEqual((tombstone.version, tombstone.record["deleted"], tombstone.record["message"]), ("deleted:1700000500.000400", True, None))
        self.assertEqual(tombstone.record["channel"]["id"], CHANNEL)
        self.assertEqual((fake.count("users.info"), fake.count("conversations.replies")), (0, 0))

    def test_lookups_that_find_nothing_leave_the_context_empty(self):
        fake = FakeSlackApi(users={}, threads={})
        records, _ = self.run_enrich(callback(message("1700000200.000100", thread_ts=PARENT["ts"])), fake=fake)
        reply = records[-1]
        self.assertIsNone(reply.record["user"])
        self.assertIsNone(reply.record["thread_parent"])
        self.assertEqual([r.kind for r in records], ["channel", "message"])

    def test_a_bot_message_skips_the_user_lookup(self):
        bot = message("1700000200.000100", subtype="bot_message", bot_id="B0BOT")
        del bot["user"]
        records, fake = self.run_enrich(callback(bot))
        self.assertIsNone(records[-1].record["user"])
        self.assertEqual(fake.count("users.info"), 0)

    def test_auth_and_scope_errors_are_permanent(self):
        for error in ("invalid_auth", "token_revoked", "missing_scope"):
            with self.subTest(error=error), self.assertRaises(Permanent):
                self.run_enrich(callback(message("1700000200.000100")), fake=FakeSlackApi(errors={"users.info": error}))

    def test_skipped_events_become_no_records(self):
        records, fake = self.run_enrich({"type": "app_rate_limited", "team_id": TEAM, "minute_rate_limited": 1})
        self.assertEqual((records, fake.calls), ([], []))


class ConfigAndSeedTest(unittest.TestCase):
    def test_database_urls(self):
        for raw in ("postgres://u:p@h/db", "postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"):
            with self.subTest(raw=raw):
                self.assertEqual(database_urls(raw), ("postgresql+psycopg://u:p@h/db", "postgresql://u:p@h/db"))
        self.assertEqual(database_urls("postgresql://u:p@h:5432/db?sslmode=disable")[1], "postgresql://u:p@h:5432/db?sslmode=disable")
        for raw in ("", "   ", "mysql://u@h/db", "file:./data/ember.db"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                database_urls(raw)

    def test_slack_time_is_exact(self):
        self.assertEqual(slack_time("1717000001.000200").microsecond, 200)
        self.assertEqual(slack_time("1717000001"), datetime(2024, 5, 29, 16, 26, 41, tzinfo=timezone.utc))
        self.assertEqual(slack_time(1717000001), datetime(2024, 5, 29, 16, 26, 41, tzinfo=timezone.utc))
        for bad in (None, True, "", "abc", "1.2.3", {}):
            self.assertIsNone(slack_time(bad))

    def test_raw_event_rows_are_what_intake_stores(self):
        edit = callback({"type": "message", "subtype": "message_changed", "channel": CHANNEL, "ts": "1",
                         "message": message("1700000200.000100")}, event_id="Ev9")
        row = enrich.raw_event_row({"type": "slack", "body": edit}, "dev")
        self.assertEqual(
            {k: row[k] for k in ("source", "delivery_id", "account_id", "tenant", "event_name", "origin")},
            {"source": "slack", "delivery_id": "Ev9", "account_id": TEAM, "tenant": "dev",
             "event_name": "message:message_changed", "origin": "webhook"},
        )
        limited = enrich.raw_event_row({"type": "slack", "body": {"type": "app_rate_limited", "team_id": TEAM, "minute_rate_limited": 1700000000}}, "dev")
        self.assertEqual((limited["delivery_id"], limited["event_name"]), (f"rate_limited:{TEAM}:1700000000", "app_rate_limited"))
        for envelope in ({"type": "github", "body": edit}, {"type": "slack", "body": message("1")}, {"type": "slack", "body": "x"}):
            self.assertIsNone(enrich.raw_event_row(envelope, "dev"))

    def test_read_envelopes_takes_json_lists_and_jsonl(self):
        one = {"type": "slack", "body": callback(message("1"))}
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "object.json": json.dumps(one, indent=2),
                "list.json": json.dumps([one, one]),
                "lines.jsonl": json.dumps(one) + "\n\n" + json.dumps(one) + "\n",
            }
            for name, content in cases.items():
                path = Path(tmp) / name
                path.write_text(content, encoding="utf-8")
                with self.subTest(name=name):
                    self.assertEqual(list(enrich.read_envelopes(str(path))), [one] * (1 if name == "object.json" else 2))

    def test_seed_refuses_a_bad_tenant(self):
        with self.assertRaises(ConfigError):
            enrich.seed("postgresql://u:p@localhost/db", "-", "Not A Tenant")


if __name__ == "__main__":
    unittest.main()
