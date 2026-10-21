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
    """The Cache, from a dict of (kind, object_id) -> a record (stored long ago), or -> [Stored] oldest first."""

    def __init__(self, records: dict | None = None):
        self.records = dict(records or {})
        self.locked: list[tuple[str, str]] = []

    def _stored(self, kind, object_id) -> list:
        value = self.records.get((kind, object_id))
        if value is None or isinstance(value, list):
            return value or []
        return [enrich.Stored("0", slack_time(0), value, None)]

    async def latest(self, kind, object_id):
        stored = self._stored(kind, object_id)
        return stored[-1].record if stored else None

    async def history(self, kind, object_id):
        return self._stored(kind, object_id)

    async def lock(self, kind, object_id):
        self.locked.append((kind, object_id))


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

    def test_a_fetched_parent_edited_before_the_fetch_is_stamped_at_its_edit(self):
        edited = {**PARENT, "text": "Decision: use Postgres.", "edited": {"user": "U0ADA", "ts": "1700000150.000200"}}
        fake = FakeSlackApi(threads={(CHANNEL, PARENT["ts"]): [edited]})
        records, _ = self.run_enrich(callback(message("1700000200.000100", thread_ts=PARENT["ts"])), fake=fake)
        parent = by_kind(records)["message"][0]
        self.assertEqual((parent.version, parent.occurred_at), ("1700000150.000200", slack_time("1700000150.000200")))

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
        reaction = callback({"type": "reaction_added", "user": "U0ADA", "reaction": "eyes",
                             "item": {"type": "message", "channel": CHANNEL, "ts": "1"}, "event_ts": "1700000600.000100"})
        for body in (
            {"type": "app_rate_limited", "team_id": TEAM, "minute_rate_limited": 1},
            reaction,
            callback({"type": "app_uninstalled"}),
            callback({"type": "tokens_revoked", "tokens": {"bot": ["U0BOT"]}}),
        ):
            with self.subTest(body=body):
                records, fake = self.run_enrich(body)
                self.assertEqual((records, fake.calls), ([], []))


BOT = "U0BOT"


def bot_callback(event: dict, **kwargs) -> dict:
    """callback() with the authorizations Slack adds: our bot's user id."""
    return {**callback(event, **kwargs), "authorizations": [{"team_id": TEAM, "user_id": BOT, "is_bot": True}]}


class OtherEventsTest(unittest.TestCase):
    """People, channels and membership (slack-webhooks.md, Everything else)."""

    def run_enrich(self, body, fake=None, cache=None):
        fake = fake or FakeSlackApi()
        records = run_async(enrich_delivery(body, cache=cache or DictCache(), call=caller(fake)))
        return records, fake

    def test_user_change_is_the_user_as_of_updated(self):
        changed = {**USERS["U0ADA"], "real_name": "Ada L.", "updated": 1700000300}
        records, fake = self.run_enrich(callback({"type": "user_change", "user": changed, "event_ts": "1700000300.000100"}))
        [user] = records
        self.assertEqual((user.kind, user.object_id, user.version, user.record), ("user", "U0ADA", "1700000300", changed))
        self.assertEqual(user.occurred_at, slack_time(1700000300))
        self.assertEqual(fake.calls, [])

    def test_user_change_has_the_same_version_as_a_fetched_user(self):
        # users.info and user_change with the same "updated" fold into one source_records row
        context = enrich.Context(DictCache(), caller(FakeSlackApi()))
        run_async(context.user("U0ADA", slack_time(1)))
        records, _ = self.run_enrich(callback({"type": "user_change", "user": USERS["U0ADA"]}))
        self.assertEqual((records[0].object_id, records[0].version), (context.fetched[0].object_id, context.fetched[0].version))

    def test_team_join_fetches_the_profile_only_when_missing(self):
        records, fake = self.run_enrich(callback({"type": "team_join", "user": USERS["U0BEN"]}))
        self.assertEqual((records[0].object_id, records[0].record, fake.calls), ("U0BEN", USERS["U0BEN"], []))
        records, fake = self.run_enrich(callback({"type": "team_join", "user": {"id": "U0BEN"}}))
        self.assertEqual(records[0].record["profile"]["email"], "ben@widget.test")
        self.assertEqual(records[0].version, "1700000001")
        self.assertEqual(fake.count("users.info"), 1)

    def test_team_join_keeps_the_payload_when_the_lookup_finds_nothing(self):
        records, _ = self.run_enrich(callback({"type": "team_join", "user": {"id": "U0NEW"}}, event_time=1700000400),
                                     fake=FakeSlackApi(users={}))
        self.assertEqual((records[0].record, records[0].version), ({"id": "U0NEW"}, "1700000400:Ev1"))

    def test_channel_created_fetches_privacy_topic_and_purpose(self):
        sent = {"id": CHANNEL, "name": "decisions", "created": 1690000000, "creator": "U0ADA"}
        records, fake = self.run_enrich(callback({"type": "channel_created", "channel": sent}, event_time=1700000300))
        [channel] = records
        self.assertEqual((channel.kind, channel.object_id, channel.version), ("channel", CHANNEL, "1700000300:Ev1"))
        self.assertEqual(channel.occurred_at, slack_time(1700000300))
        self.assertEqual(channel.record, {**sent, **CHANNELS[CHANNEL]})
        self.assertEqual(fake.count("conversations.info"), 1)

    def test_channel_created_keeps_the_payload_when_the_lookup_finds_nothing(self):
        sent = {"id": "C0NEW", "name": "new", "created": 1700000300, "creator": "U0ADA"}
        records, _ = self.run_enrich(callback({"type": "channel_created", "channel": sent}))
        self.assertEqual(records[0].record, sent)

    def test_rename_and_archive_merge_onto_the_stored_channel(self):
        cache = DictCache({("channel", CHANNEL): CHANNELS[CHANNEL]})
        rename = callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "adr", "created": 1690000000}},
                          event_time=1700000300)
        records, fake = self.run_enrich(rename, cache=cache)
        self.assertEqual(records[0].record, {**CHANNELS[CHANNEL], "name": "adr"})
        self.assertEqual(records[0].version, "1700000300:Ev1")
        self.assertEqual(cache.locked, [("channel", CHANNEL)])  # one change to a channel at a time
        for kind, archived in (("channel_archive", True), ("group_archive", True), ("channel_unarchive", False), ("group_unarchive", False)):
            with self.subTest(kind=kind):
                records, fake = self.run_enrich(callback({"type": kind, "channel": CHANNEL, "user": "U0ADA"}), cache=cache)
                self.assertEqual(records[0].record, {**CHANNELS[CHANNEL], "is_archived": archived})
                self.assertEqual(fake.calls, [])

    def test_a_change_to_an_unstored_channel_fetches_it_first(self):
        records, fake = self.run_enrich(callback({"type": "group_rename", "channel": {"id": CHANNEL, "name": "adr"}}))
        self.assertEqual([r.kind for r in records], ["channel"])  # one record, not a fetched copy as well
        self.assertEqual(records[0].record, {**CHANNELS[CHANNEL], "name": "adr"})
        self.assertEqual(fake.count("conversations.info"), 1)

    def test_channel_deleted_is_a_tombstone_and_fetches_nothing(self):
        records, fake = self.run_enrich(callback({"type": "channel_deleted", "channel": CHANNEL}),
                                        cache=DictCache({("channel", CHANNEL): CHANNELS[CHANNEL]}))
        self.assertEqual(records[0].record, {**CHANNELS[CHANNEL], "is_deleted": True})
        records, fake = self.run_enrich(callback({"type": "channel_deleted", "channel": "C0GONE"}))
        self.assertEqual(records[0].record, {"id": "C0GONE", "is_deleted": True})
        self.assertEqual(fake.calls, [])

    def test_a_late_change_is_carried_onto_the_newest_channel(self):
        # The archive (t=400) was enriched before a rename (t=300) that Slack sent earlier arrived
        archive = callback({"type": "channel_archive", "channel": CHANNEL, "user": "U0ADA"}, event_id="Ev2", event_time=1700000400)
        cache = DictCache({("channel", CHANNEL): [
            enrich.Stored("1700000000123", slack_time(1700000000), CHANNELS[CHANNEL], None),
            enrich.Stored("1700000400:Ev2", slack_time(1700000400), {**CHANNELS[CHANNEL], "is_archived": True}, archive),
        ]})
        rename = callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "adr"}}, event_time=1700000300)
        records, fake = self.run_enrich(rename, cache=cache)
        as_of_rename, carried = records
        self.assertEqual((as_of_rename.version, as_of_rename.occurred_at), ("1700000300:Ev1", slack_time(1700000300)))
        self.assertEqual(as_of_rename.record, {**CHANNELS[CHANNEL], "name": "adr"})  # not archived yet
        self.assertEqual((carried.version, carried.occurred_at), ("1700000400:Ev2+1700000300:Ev1", slack_time(1700000400)))
        self.assertEqual(carried.record, {**CHANNELS[CHANNEL], "name": "adr", "is_archived": True})
        self.assertEqual(fake.calls, [])

        # Running it again carries nothing new: the carried record is already the newest
        cache.records[("channel", CHANNEL)].append(enrich.Stored(carried.version, carried.occurred_at, carried.record, rename))
        records, _ = self.run_enrich(rename, cache=cache)
        self.assertEqual([r.version for r in records], ["1700000300:Ev1"])

    def test_a_late_change_does_not_undo_a_newer_one(self):
        newer = callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "newest"}}, event_id="Ev2", event_time=1700000400)
        cache = DictCache({("channel", CHANNEL): [
            enrich.Stored("1700000000123", slack_time(1700000000), CHANNELS[CHANNEL], None),
            enrich.Stored("1700000400:Ev2", slack_time(1700000400), {**CHANNELS[CHANNEL], "name": "newest"}, newer),
        ]})
        late = callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "older"}}, event_time=1700000300)
        records, _ = self.run_enrich(late, cache=cache)
        self.assertEqual([(r.version, r.record["name"]) for r in records], [("1700000300:Ev1", "older")])

    def test_two_events_in_one_second_are_two_versions(self):
        joined = callback({"type": "member_joined_channel", "user": "U0BEN", "channel": CHANNEL}, event_id="Ev1")
        left = callback({"type": "member_left_channel", "user": "U0BEN", "channel": CHANNEL}, event_id="Ev2")
        cache = DictCache({("user", "U0BEN"): USERS["U0BEN"]})
        [join_record], _ = self.run_enrich(joined, cache=cache)
        [leave_record], _ = self.run_enrich(left, cache=cache)
        self.assertEqual((join_record.version, leave_record.version), ("1700000200:Ev1", "1700000200:Ev2"))

    def test_topic_purpose_and_name_messages_update_the_channel(self):
        cache = DictCache({("channel", CHANNEL): CHANNELS[CHANNEL]})
        ts = "1700000300.000100"
        for subtype, extra, expected in (
            ("channel_topic", {"topic": "Ship v2"}, {"topic": {"value": "Ship v2", "creator": "U0ADA", "last_set": 1700000300}}),
            ("group_purpose", {"purpose": "Decisions"}, {"purpose": {"value": "Decisions", "creator": "U0ADA", "last_set": 1700000300}}),
            ("channel_name", {"name": "adr", "old_name": "decisions"}, {"name": "adr"}),
        ):
            with self.subTest(subtype=subtype):
                body = callback(message(ts, subtype=subtype, **extra))
                records, fake = self.run_enrich(body, cache=DictCache({**cache.records, ("user", "U0ADA"): USERS["U0ADA"]}))
                channel, msg = records
                self.assertEqual((channel.kind, channel.object_id, channel.version), ("channel", CHANNEL, ts))
                self.assertEqual(channel.record, {**CHANNELS[CHANNEL], **expected})
                self.assertEqual((msg.kind, msg.record["channel"]), ("message", channel.record))  # the context is the new channel
                self.assertEqual(fake.calls, [])
        # A channel not stored yet is fetched first, as for a channel event
        records, fake = self.run_enrich(callback(message(ts, subtype="channel_topic", topic="Ship v2")))
        self.assertEqual(by_kind(records)["channel"][0].record["name"], "decisions")
        self.assertEqual(fake.count("conversations.info"), 1)
        # A join is only a message
        records, _ = self.run_enrich(callback(message(ts, subtype="channel_join")))
        self.assertEqual(sorted(by_kind(records)), ["channel", "message", "user"])  # the fetched context, no change
        self.assertEqual(by_kind(records)["channel"][0].version, "1700000000123")

    def test_member_joined_fetches_the_person_once(self):
        joined = callback({"type": "member_joined_channel", "user": "U0BEN", "channel": CHANNEL,
                           "channel_type": "C", "team": TEAM, "inviter": "U0ADA"}, event_time=1700000300)
        records, fake = self.run_enrich(joined)
        user, membership = records
        self.assertEqual((user.kind, user.object_id), ("user", "U0BEN"))
        self.assertEqual((membership.kind, membership.object_id, membership.version),
                         ("membership", f"{CHANNEL}/U0BEN", "1700000300:Ev1"))
        self.assertEqual(membership.record, {"payload": joined, "user": USERS["U0BEN"], "channel": CHANNEL,
                                             "member": True, "inviter": "U0ADA"})
        records, fake = self.run_enrich(joined, cache=DictCache({("user", "U0BEN"): USERS["U0BEN"]}))
        self.assertEqual(([r.kind for r in records], fake.calls), (["membership"], []))

    def test_member_left_is_a_tombstone_and_fetches_nothing(self):
        left = callback({"type": "member_left_channel", "user": "U0BEN", "channel": CHANNEL, "channel_type": "C"})
        records, fake = self.run_enrich(left)
        [membership] = records
        self.assertEqual((membership.record["member"], membership.record["user"], membership.record["inviter"]), (False, None, None))
        self.assertEqual(fake.calls, [])

    def test_the_bot_joining_or_leaving_is_logged(self):
        for kind, words in (("member_joined_channel", "pull_test.py --channel"), ("member_left_channel", "records are kept")):
            with self.subTest(kind=kind), self.assertLogs("slack-enrich", "INFO") as logs:
                records, _ = self.run_enrich(bot_callback({"type": kind, "user": BOT, "channel": CHANNEL}),
                                             fake=FakeSlackApi(users={BOT: {"id": BOT, "updated": 1}}))
                self.assertEqual(records[-1].object_id, f"{CHANNEL}/{BOT}")
                self.assertTrue(any(words in line for line in logs.output), logs.output)

    def test_events_missing_what_they_need_are_permanent(self):
        for event in (
            {"type": "user_change", "user": {"name": "no id"}},
            {"type": "team_join"},
            {"type": "channel_rename", "channel": {"name": "no id"}},
            {"type": "channel_archive"},
            {"type": "member_joined_channel", "channel": CHANNEL},
            {"type": "member_left_channel", "user": "U0ADA"},
        ):
            with self.subTest(event=event), self.assertRaises(Permanent):
                self.run_enrich(callback(event))
        body = callback({"type": "channel_deleted", "channel": CHANNEL})
        del body["event_time"]
        with self.assertRaises(Permanent):
            self.run_enrich(body)

    def test_auth_errors_on_a_lookup_are_permanent(self):
        with self.assertRaises(Permanent):
            self.run_enrich(callback({"type": "channel_created", "channel": {"id": CHANNEL}}),
                            fake=FakeSlackApi(errors={"conversations.info": "invalid_auth"}))


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
        self.assertEqual(row["occurred_at"], slack_time("1"))  # message_changed: event.ts, when it was edited
        limited = enrich.raw_event_row({"type": "slack", "body": {"type": "app_rate_limited", "team_id": TEAM, "minute_rate_limited": 1700000000}}, "dev")
        self.assertEqual((limited["delivery_id"], limited["event_name"]), (f"rate_limited:{TEAM}:1700000000", "app_rate_limited"))
        self.assertEqual(limited["occurred_at"], slack_time(1700000000))
        for envelope in ({"type": "github", "body": edit}, {"type": "slack", "body": message("1")}, {"type": "slack", "body": "x"}):
            self.assertIsNone(enrich.raw_event_row(envelope, "dev"))

    def test_occurred_at_follows_the_slack_ordering_rule(self):
        # slack-webhooks.md, Ordering value: event.event_ts, then event.ts, then the wrapper's event_time
        cases = (
            (callback(message("1700000200.000100")), "1700000200.000100"),
            (callback({"type": "message", "subtype": "message_deleted", "channel": CHANNEL, "ts": "1700000500.000400",
                       "deleted_ts": "1700000200.000100"}), "1700000500.000400"),
            (callback({"type": "channel_rename", "channel": {"id": CHANNEL, "name": "adr"}}, event_time=1700000300), 1700000300),
        )
        for body, expected in cases:
            with self.subTest(event=body["event"]["type"]):
                self.assertEqual(enrich.ordering_value(body), slack_time(expected))
        no_time = callback({"type": "channel_deleted", "channel": CHANNEL})
        del no_time["event_time"]
        self.assertNotIn("occurred_at", enrich.raw_event_row({"type": "slack", "body": no_time}, "dev"))  # the column's default

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
