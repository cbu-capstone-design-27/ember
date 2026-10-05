"""The local Slack pull turns a fake workspace into valid {type, body} JSONL.

No test here calls Slack. A FakeSlack answers Web API GETs from memory.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pull_test  # noqa: E402
from pull_test import MethodGates, PullError, Response, SlackApiError  # noqa: E402
from validate import assert_valid  # noqa: E402

TOKEN = "xoxb-test-token"

TEAM = {"id": "T0TEAM", "name": "Widget Co", "domain": "widget", "email_domain": "widget.test"}
USERS = [
    {"id": "U0ADA", "name": "ada", "real_name": "Ada", "is_bot": False, "profile": {"display_name": "Ada", "email": "ada@widget.test"}},
    {"id": "U0BOT", "name": "ember", "real_name": "Ember", "is_bot": True, "profile": {"display_name": ""}},
]
CHANNELS = [
    {"id": "C0DEC", "name": "decisions", "is_channel": True, "is_private": False, "is_member": True},
    {"id": "C0NOT", "name": "random", "is_channel": True, "is_private": False, "is_member": False},
    {"id": "G0PRIV", "name": "core-team", "is_channel": True, "is_private": True, "is_member": True},
]
# conversations.history is newest first.
HISTORY = {
    "C0DEC": [
        {"type": "message", "user": "U0ADA", "text": "Follow-up", "ts": "1700000300.000100"},
        {
            "type": "message",
            "subtype": "thread_broadcast",
            "user": "U0ADA",
            "text": "Also sent to channel",
            "ts": "1700000250.000100",
            "thread_ts": "1700000100.000100",
        },
        {
            "type": "message",
            "user": "U0ADA",
            "text": "Decision: use Neo4j for the graph.",
            "ts": "1700000100.000100",
            "thread_ts": "1700000100.000100",
            "reply_count": 2,
        },
    ],
    "G0PRIV": [{"type": "message", "user": "U0ADA", "text": "Private note", "ts": "1700000500.000100"}],
}
REPLIES = {
    ("C0DEC", "1700000100.000100"): [
        {"type": "message", "user": "U0ADA", "text": "Decision: use Neo4j for the graph.", "ts": "1700000100.000100", "thread_ts": "1700000100.000100", "reply_count": 2},
        {"type": "message", "user": "U0ADA", "text": "Because of Cypher.", "ts": "1700000200.000100", "thread_ts": "1700000100.000100"},
        {"type": "message", "subtype": "thread_broadcast", "user": "U0ADA", "text": "Also sent to channel", "ts": "1700000250.000100", "thread_ts": "1700000100.000100"},
    ]
}


def _json(payload, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, headers or {}, json.dumps(payload).encode("utf-8"))


class FakeSlack:
    """Answers GET https://slack.com/api/<method>?... from the tables above.

    page_size splits list results into cursor pages. throttle maps a method
    name to how many 429s it answers first. errors maps a method name (or
    (method, channel)) to an ok:false error.
    """

    def __init__(self, *, page_size: int = 1, throttle: dict | None = None, errors: dict | None = None):
        self.page_size = page_size
        self.throttle = dict(throttle or {})
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, dict]] = []
        self.auth_headers: list[str] = []
        self._lock = threading.Lock()

    def _page(self, items: list, params: dict, field: str) -> Response:
        start = int(params.get("cursor", "0") or 0)
        chunk = items[start : start + self.page_size]
        following = start + self.page_size
        meta = {"next_cursor": str(following) if following < len(items) else ""}
        return _json({"ok": True, field: chunk, "response_metadata": meta})

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        api_method = parts.path.rsplit("/", 1)[-1]
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        with self._lock:
            self.calls.append((api_method, params))
            self.auth_headers.append(headers.get("Authorization", ""))
            if self.throttle.get(api_method):
                self.throttle[api_method] -= 1
                return _json({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "7"})
        assert method == "GET" and body is None
        for key in ((api_method, params.get("channel")), api_method):
            if key in self.errors:
                error = self.errors[key]
                payload = {"ok": False, "error": error}
                if error == "missing_scope":
                    payload["needed"] = "groups:history"
                return _json(payload)
        if api_method == "team.info":
            return _json({"ok": True, "team": dict(TEAM)})
        if api_method == "users.list":
            return self._page([dict(u) for u in USERS], params, "members")
        if api_method == "conversations.list":
            channels = [dict(c) for c in CHANNELS]
            return self._page(channels, params, "channels")
        if api_method == "conversations.history":
            messages = [dict(m) for m in HISTORY.get(params["channel"], [])]
            oldest = params.get("oldest")
            if oldest:
                messages = [m for m in messages if float(m["ts"]) >= float(oldest)]
            return self._page(messages, params, "messages")
        if api_method == "conversations.replies":
            messages = [dict(m) for m in REPLIES.get((params["channel"], params["ts"]), [])]
            return self._page(messages, params, "messages")
        return _json({"ok": False, "error": "unknown_method"}, 404)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []
        self._lock = threading.Lock()

    def clock(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.slept.append(seconds)
            self.now += seconds


def run(fake: FakeSlack, **kwargs) -> list[dict]:
    clock = kwargs.pop("clock", None) or FakeClock()
    gates = MethodGates(clock.clock, clock.sleep)
    return list(pull_test.pull(TOKEN, fake, gates=gates, **kwargs))


def bodies(envelopes: list[dict]) -> list[dict]:
    return [env["body"] for env in envelopes]


class PullFlowTest(unittest.TestCase):
    def test_every_line_is_a_valid_envelope(self):
        envelopes = run(FakeSlack())
        self.assertTrue(envelopes)
        for env in envelopes:
            self.assertEqual(set(env), {"type", "body"})
            self.assertEqual(env["type"], "slack")
            assert_valid(env)

    def test_output_order_team_users_then_channels_with_messages(self):
        got = bodies(run(FakeSlack()))
        texts = [(b.get("id") or b.get("text")) for b in got]
        self.assertEqual(
            texts,
            [
                "T0TEAM",
                "U0ADA",
                "U0BOT",
                "C0DEC",
                "Decision: use Neo4j for the graph.",
                "Because of Cypher.",
                "Also sent to channel",
                "Follow-up",
                "G0PRIV",
                "Private note",
            ],
        )

    def test_only_channels_the_bot_is_in(self):
        fake = FakeSlack()
        got = bodies(run(fake))
        self.assertNotIn("C0NOT", [b.get("id") for b in got])
        read = {params["channel"] for method, params in fake.calls if method == "conversations.history"}
        self.assertEqual(read, {"C0DEC", "G0PRIV"})

    def test_lists_public_and_private_channels_without_archived(self):
        fake = FakeSlack()
        run(fake)
        params = [p for m, p in fake.calls if m == "conversations.list"][0]
        self.assertEqual(params["types"], "public_channel,private_channel")
        self.assertEqual(params["exclude_archived"], "true")
        fake = FakeSlack()
        run(fake, include_archived=True)
        params = [p for m, p in fake.calls if m == "conversations.list"][0]
        self.assertEqual(params["exclude_archived"], "false")

    def test_messages_get_their_channel_and_are_otherwise_raw(self):
        got = bodies(run(FakeSlack()))
        messages = [b for b in got if b.get("type") == "message"]
        self.assertTrue(all("channel" in m for m in messages))
        decision = next(m for m in messages if m["text"].startswith("Decision"))
        self.assertEqual(
            decision,
            {**HISTORY["C0DEC"][2], "channel": "C0DEC"},
        )
        private = next(m for m in messages if m["text"] == "Private note")
        self.assertEqual(private["channel"], "G0PRIV")

    def test_existing_channel_field_is_not_overwritten(self):
        HISTORY["C0DEC"].append({"type": "message", "text": "Shared", "ts": "1700000001.000100", "channel": "C0ELSEWHERE"})
        try:
            got = bodies(run(FakeSlack()))
        finally:
            HISTORY["C0DEC"].pop()
        shared = next(b for b in got if b.get("text") == "Shared")
        self.assertEqual(shared["channel"], "C0ELSEWHERE")

    def test_replies_only_for_thread_parents_and_parent_not_repeated(self):
        fake = FakeSlack()
        got = bodies(run(fake))
        reply_calls = [p for m, p in fake.calls if m == "conversations.replies"]
        self.assertEqual({(p["channel"], p["ts"]) for p in reply_calls}, {("C0DEC", "1700000100.000100")})
        texts = [b.get("text") for b in got]
        self.assertEqual(texts.count("Decision: use Neo4j for the graph."), 1)
        self.assertEqual(texts.count("Also sent to channel"), 1)

    def test_cursor_pages_are_followed(self):
        fake = FakeSlack(page_size=1)
        run(fake)
        cursors = [p.get("cursor") for m, p in fake.calls if m == "users.list"]
        self.assertEqual(cursors, [None, "1"])
        self.assertTrue(all(p["limit"] == str(pull_test.PAGE_LIMIT) for m, p in fake.calls if "cursor" in p or m.endswith(".list")))

    def test_since_is_sent_as_oldest(self):
        fake = FakeSlack()
        got = bodies(run(fake, oldest=pull_test.parse_since("1700000260")))
        texts = [b.get("text") for b in got if b.get("type") == "message"]
        self.assertEqual(texts, ["Follow-up", "Private note"])
        for method, params in fake.calls:
            if method == "conversations.history":
                self.assertEqual(params["oldest"], "1700000260.000000")

    def test_one_channel_run(self):
        fake = FakeSlack()
        got = bodies(run(fake, only_channel="G0PRIV"))
        self.assertEqual([b.get("id") or b.get("text") for b in got][-2:], ["G0PRIV", "Private note"])
        self.assertNotIn("C0DEC", [b.get("id") for b in got])

    def test_one_channel_run_needs_membership(self):
        with self.assertRaises(PullError) as caught:
            run(FakeSlack(), only_channel="C0NOT")
        self.assertIn("/invite @Ember", str(caught.exception))
        with self.assertRaises(PullError) as caught:
            run(FakeSlack(), only_channel="C0MISSING")
        self.assertIn("not found", str(caught.exception))

    def test_token_is_only_in_the_authorization_header(self):
        fake = FakeSlack()
        run(fake)
        self.assertTrue(all(h == f"Bearer {TOKEN}" for h in fake.auth_headers))
        self.assertFalse(any(TOKEN in json.dumps(params) for _m, params in fake.calls))


class ErrorHandlingTest(unittest.TestCase):
    def test_bad_token_stops_with_a_clear_message(self):
        with self.assertRaises(SlackApiError) as caught:
            run(FakeSlack(errors={"team.info": "invalid_auth"}))
        self.assertIn("SLACK_BOT_TOKEN was rejected", str(caught.exception))

    def test_missing_scope_names_the_scope(self):
        with self.assertRaises(SlackApiError) as caught:
            run(FakeSlack(errors={("conversations.history", "G0PRIV"): "missing_scope"}))
        self.assertIn("groups:history", str(caught.exception))

    def test_channel_the_bot_left_is_skipped(self):
        fake = FakeSlack(errors={("conversations.history", "C0DEC"): "not_in_channel"})
        got = bodies(run(fake))
        texts = [b.get("text") for b in got if b.get("type") == "message"]
        self.assertEqual(texts, ["Private note"])

    def test_http_error_stops_the_pull(self):
        def broken(method, url, headers, body):
            return Response(404, {}, b"nope")

        with self.assertRaises(PullError):
            run(broken)


class RateLimitTest(unittest.TestCase):
    def test_retry_after_is_waited_per_method(self):
        clock = FakeClock()
        fake = FakeSlack(throttle={"conversations.history": 1})
        got = run(fake, clock=clock)
        self.assertEqual(clock.slept, [7.0])
        self.assertEqual(len(got), len(run(FakeSlack())))

    def test_waits_out_repeated_throttles(self):
        clock = FakeClock()
        got = run(FakeSlack(throttle={"team.info": 10}), clock=clock)
        self.assertEqual(clock.slept, [7.0] * 10)
        self.assertEqual(len(got), len(run(FakeSlack())))

    def test_gives_up_only_after_the_throttle_budget(self):
        clock = FakeClock()
        client = pull_test.SlackClient(
            "xoxb-test-token",
            FakeSlack(throttle={"team.info": 1000}),
            MethodGates(clock.clock, clock.sleep),
            throttle_budget=60.0,
        )
        with self.assertRaises(PullError) as caught:
            client.call("team.info")
        self.assertIn("still throttled", str(caught.exception))
        self.assertEqual(clock.slept, [7.0] * 8)  # 8 x 7s fits in 60s; the 9th would not

    def test_retry_after_seconds(self):
        self.assertIsNone(pull_test.retry_after_seconds(Response(200, {}, b"")))
        self.assertEqual(pull_test.retry_after_seconds(Response(429, {"Retry-After": "30"}, b"")), 30.0)
        self.assertEqual(pull_test.retry_after_seconds(Response(429, {}, b"")), 1.0)
        self.assertEqual(pull_test.retry_after_seconds(Response(503, {}, b""), attempt=2), 4.0)
        self.assertIsNone(pull_test.retry_after_seconds(Response(404, {}, b"")))

    def test_gates_are_separate_per_method(self):
        clock = FakeClock()
        gates = MethodGates(clock.clock, clock.sleep)
        gates["conversations.history"].extend(10)
        gates["users.list"].wait()
        self.assertEqual(clock.slept, [])
        gates["conversations.history"].wait()
        self.assertEqual(clock.slept, [10.0])


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("SLACK_BOT_TOKEN", "SLACK_TEST_CHANNEL", "SLACK_PULL_CONCURRENCY")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_bot_token_must_be_a_bot_token(self):
        os.environ.pop("SLACK_BOT_TOKEN", None)
        with self.assertRaises(PullError):
            pull_test.bot_token()
        for wrong in ("xoxp-user", "xapp-1-app"):
            os.environ["SLACK_BOT_TOKEN"] = wrong
            with self.assertRaises(PullError):
                pull_test.bot_token()
        os.environ["SLACK_BOT_TOKEN"] = " xoxb-ok "
        self.assertEqual(pull_test.bot_token(), "xoxb-ok")
        os.environ["SLACK_BOT_TOKEN"] = "xoxe.xoxb-rotated"
        self.assertEqual(pull_test.bot_token(), "xoxe.xoxb-rotated")

    def test_channel_resolution(self):
        os.environ.pop("SLACK_TEST_CHANNEL", None)
        self.assertIsNone(pull_test.resolve_channel(None))
        os.environ["SLACK_TEST_CHANNEL"] = "C0ENV"
        self.assertEqual(pull_test.resolve_channel(None), "C0ENV")
        self.assertEqual(pull_test.resolve_channel("G0CLI"), "G0CLI")
        with self.assertRaises(PullError):
            pull_test.resolve_channel("#general")

    def test_concurrency_bounds(self):
        os.environ.pop("SLACK_PULL_CONCURRENCY", None)
        self.assertEqual(pull_test.resolve_concurrency(None), pull_test.DEFAULT_CONCURRENCY)
        os.environ["SLACK_PULL_CONCURRENCY"] = "8"
        self.assertEqual(pull_test.resolve_concurrency(None), 8)
        for bad in (0, pull_test.MAX_CONCURRENCY + 1):
            with self.assertRaises(PullError):
                pull_test.resolve_concurrency(bad)
        os.environ["SLACK_PULL_CONCURRENCY"] = "many"
        with self.assertRaises(PullError):
            pull_test.resolve_concurrency(None)

    def test_parse_since(self):
        self.assertIsNone(pull_test.parse_since(None))
        self.assertEqual(pull_test.parse_since("2023-11-14"), "1699920000.000000")
        self.assertEqual(pull_test.parse_since("1700000000.5"), "1700000000.500000")
        with self.assertRaises(PullError):
            pull_test.parse_since("last week")


class ReadableOutputTest(unittest.TestCase):
    def test_summary_names_people_channels_and_threads(self):
        summary = pull_test.render_summary(run(FakeSlack()))
        self.assertIn("# Slack: Widget Co", summary)
        self.assertIn("## Channels (2)", summary)
        self.assertIn("#decisions (C0DEC, public)", summary)
        self.assertIn("#core-team (G0PRIV, private)", summary)
        self.assertIn("Ember (U0BOT) bot", summary)
        self.assertIn("#decisions 2023-11-14 22:15 Ada — Decision: use Neo4j for the graph. (2 replies)", summary)
        self.assertIn("  ↳ #decisions", summary)
        self.assertIn("Because of Cypher.", summary)

    def test_summary_from_file_matches_in_memory(self):
        envelopes = run(FakeSlack())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intake.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                pull_test.emit_jsonl(envelopes, handle)
            self.assertEqual(pull_test.render_summary_file(path), pull_test.render_summary(envelopes))

    def test_main_writes_jsonl_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            saved_exchange = pull_test.urllib_exchange
            saved_gates = pull_test.MethodGates
            os.environ["SLACK_BOT_TOKEN"] = TOKEN
            os.environ.pop("SLACK_TEST_CHANNEL", None)
            clock = FakeClock()
            pull_test.urllib_exchange = FakeSlack()
            pull_test.MethodGates = lambda **kw: saved_gates(clock.clock, clock.sleep)
            err = io.StringIO()
            try:
                saved_err, sys.stderr = sys.stderr, err
                code = pull_test.main(["--output", str(output)])
            finally:
                sys.stderr = saved_err
                pull_test.urllib_exchange = saved_exchange
                pull_test.MethodGates = saved_gates
                os.environ.pop("SLACK_BOT_TOKEN", None)
            self.assertEqual(code, 0, err.getvalue())
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 10)
            for line in lines:
                assert_valid(json.loads(line))
            sidecar = pull_test.readable_sidecar(output)
            self.assertTrue(sidecar.read_text(encoding="utf-8").startswith("# Slack: Widget Co"))
            self.assertNotIn(TOKEN, err.getvalue())

    def test_main_rejects_conflicting_readable_flags(self):
        self.assertEqual(pull_test.main(["--readable", "x", "--no-readable"]), 2)
        self.assertEqual(pull_test.main(["--readable", "-"]), 2)



# --- EMBER-52: backoff, pacing, no page ceiling, checkpoint and resume ------------------


class Crash(Exception):
    """Stands in for the process dying mid-backfill."""


class BigSlack:
    """One workspace, one big channel, with injectable faults.

    ``messages`` top-level messages; every ``thread_every``-th is a thread
    parent with ``replies_per_thread`` replies. ``faults`` maps a 1-based
    call number to "429", "ratelimited", "503" or "network". ``crash_at``
    raises Crash on that call. ``force_page`` overrides the page size.
    """

    def __init__(self, *, messages=10_000, thread_every=50, replies_per_thread=3, faults=None,
                 crash_at=None, force_page=None, repeat_cursor=False, always=None):
        self.faults = dict(faults or {})
        self.crash_at = crash_at
        self.force_page = force_page
        self.repeat_cursor = repeat_cursor
        self.always = always
        self.calls = 0
        self.method_calls: dict[str, int] = {}
        self.limits: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self.history = []  # newest first, as Slack returns it
        self.replies = {}
        for i in range(messages):
            base = 1_700_000_000 + i
            ts = f"{base}.000100"
            message = {"type": "message", "user": "U0ADA", "text": f"message {i}", "ts": ts}
            if i % thread_every == 0 and replies_per_thread:
                message.update(thread_ts=ts, reply_count=replies_per_thread)
                self.replies[ts] = [dict(message)] + [
                    {"type": "message", "user": "U0ADA", "text": f"reply {i}.{r}",
                     "ts": f"{base}.{101 + r:06d}", "thread_ts": ts}
                    for r in range(replies_per_thread)
                ]
            self.history.append(message)
        self.history.reverse()

    def expected_message_ts(self) -> set:
        expected = {m["ts"] for m in self.history}
        for thread in self.replies.values():
            expected.update(r["ts"] for r in thread)
        return expected

    def _page(self, items, params, field):
        size = self.force_page or int(params.get("limit", "200"))
        start = int(params.get("cursor", "0") or 0)
        chunk = [dict(item) for item in items[start:start + size]]
        following = start + size
        if self.repeat_cursor and start > 0:
            following = start
        meta = {"next_cursor": str(following) if following < len(items) else ""}
        return _json({"ok": True, field: chunk, "response_metadata": meta})

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        api_method = parts.path.rsplit("/", 1)[-1]
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        with self._lock:
            self.calls += 1
            call = self.calls
            self.method_calls[api_method] = self.method_calls.get(api_method, 0) + 1
            if "limit" in params:
                self.limits.append((api_method, int(params["limit"])))
        if self.crash_at is not None and call == self.crash_at:
            raise Crash(f"crash at call {call}")
        fault = self.always or self.faults.get(call)
        if fault == "429":
            return _json({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "3"})
        if fault == "ratelimited":
            return _json({"ok": False, "error": "ratelimited"}, 200, {"Retry-After": "2"})
        if fault == "503":
            return _json({"ok": False}, 503)
        if fault == "network":
            return Response(pull_test.NETWORK_ERROR, {}, b"ConnectionResetError: reset by peer")
        if api_method == "team.info":
            return _json({"ok": True, "team": dict(TEAM)})
        if api_method == "users.list":
            return self._page([dict(USERS[0])], params, "members")
        if api_method == "conversations.list":
            channel = {"id": "C0BIG", "name": "big", "is_channel": True, "is_private": False, "is_member": True}
            return self._page([channel], params, "channels")
        if api_method == "conversations.history":
            return self._page(self.history, params, "messages")
        if api_method == "conversations.replies":
            return self._page(self.replies.get(params["ts"], []), params, "messages")
        return _json({"ok": False, "error": "unknown_method"}, 404)


def backfill(fake, output: Path, clock: FakeClock | None = None, **kwargs) -> int:
    clock = clock or FakeClock()
    gates = kwargs.pop("gates", None) or MethodGates(clock.clock, clock.sleep)
    return pull_test.pull_to_file(TOKEN, fake, output, gates=gates, jitter=lambda: 1.0, **kwargs)


def read_output(output: Path) -> list:
    return [json.loads(line)["body"] for line in output.read_text(encoding="utf-8").splitlines()]


def assert_complete(test: unittest.TestCase, fake: BigSlack, output: Path) -> None:
    got = read_output(output)
    test.assertEqual(got[0]["id"], "T0TEAM")
    test.assertEqual(got[2]["id"], "C0BIG")
    ts = [b["ts"] for b in got if b.get("type") == "message"]
    test.assertEqual(len(ts), len(set(ts)), "a message was written twice")
    test.assertEqual(set(ts), fake.expected_message_ts(), "a message is missing")
    test.assertTrue(all(b.get("channel") == "C0BIG" for b in got if b.get("type") == "message"))
    test.assertFalse(pull_test.checkpoint_path(output).exists())
    test.assertFalse(pull_test.parts_dir(output).exists())


class FullBackfillTest(unittest.TestCase):
    """EMBER-52: a full channel backfill completes without failure."""

    def test_ten_thousand_messages_through_throttles_and_errors(self):
        faults = {3: "429", 6: "ratelimited", 9: "503", 10: "network", 20: "429", 21: "429",
                  60: "network", 61: "503", 62: "network", 150: "ratelimited", 200: "429"}
        fake = BigSlack(faults=faults)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            count = backfill(fake, output, clock=clock)
            assert_complete(self, fake, output)
            self.assertEqual(count, 3 + len(fake.expected_message_ts()))
        self.assertGreaterEqual(len(fake.expected_message_ts()), 10_000)
        self.assertIn(3.0, clock.slept)  # 429 Retry-After honored
        self.assertIn(2.0, clock.slept)  # "ratelimited" body Retry-After honored

    def test_output_order_matches_the_in_memory_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            backfill(BigSlack(messages=120, thread_every=10), output)
            on_disk = read_output(output)
        clock = FakeClock()
        in_memory = [env["body"] for env in pull_test.pull(
            TOKEN, BigSlack(messages=120, thread_every=10), gates=MethodGates(clock.clock, clock.sleep))]
        self.assertEqual(on_disk, in_memory)

    def test_more_than_five_hundred_pages(self):
        fake = BigSlack(messages=601, replies_per_thread=0, force_page=1)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            backfill(fake, output)
            assert_complete(self, fake, output)
        self.assertGreater(fake.method_calls["conversations.history"], 500)

    def test_repeated_cursor_still_stops(self):
        fake = BigSlack(messages=10, replies_per_thread=0, force_page=1, repeat_cursor=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PullError) as caught:
                backfill(fake, Path(tmp) / "intake.jsonl")
        self.assertIn("repeated its cursor", str(caught.exception))


class ResumeTest(unittest.TestCase):
    def crash_then_resume(self, crash_at: int) -> BigSlack:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            with self.assertRaises(Crash):
                backfill(BigSlack(crash_at=crash_at), output)
            self.assertTrue(pull_test.checkpoint_path(output).exists())
            self.assertFalse(output.exists())
            second = BigSlack()
            backfill(second, output, resume=True)
            assert_complete(self, second, output)
            return second

    def test_crash_during_history_resumes_from_the_saved_cursor(self):
        second = self.crash_then_resume(crash_at=20)  # 3 setup calls, then history page 17
        self.assertLess(second.method_calls["conversations.history"], 50)

    def test_crash_during_replies_skips_finished_history_and_threads(self):
        second = self.crash_then_resume(crash_at=150)  # 3 setup + 50 history, then ~97 threads
        self.assertNotIn("conversations.history", second.method_calls)
        self.assertLess(second.method_calls["conversations.replies"], 200)

    def test_unfinished_run_needs_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            with self.assertRaises(Crash):
                backfill(BigSlack(crash_at=30), output)
            with self.assertRaises(PullError) as caught:
                backfill(BigSlack(), output)
            self.assertIn("--resume", str(caught.exception))

    def test_resume_with_different_options_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            with self.assertRaises(Crash):
                backfill(BigSlack(crash_at=30), output)
            with self.assertRaises(PullError) as caught:
                backfill(BigSlack(), output, resume=True, oldest="1700000500.000000")
            self.assertIn("different options", str(caught.exception))

    def test_resume_without_a_checkpoint_starts_fresh(self):
        fake = BigSlack(messages=50)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            backfill(fake, output, resume=True)
            assert_complete(self, fake, output)


class BackoffAndPacingTest(unittest.TestCase):
    def test_transient_errors_back_off_then_give_up(self):
        clock = FakeClock()
        client = pull_test.SlackClient(TOKEN, BigSlack(always="503"), MethodGates(clock.clock, clock.sleep),
                                       jitter=lambda: 1.0)
        with self.assertRaises(PullError) as caught:
            client.call("team.info")
        self.assertIn("failed after 8 attempts", str(caught.exception))
        self.assertEqual(clock.slept, [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0])

    def test_jitter_shortens_backoff(self):
        clock = FakeClock()
        client = pull_test.SlackClient(TOKEN, BigSlack(always="network"), MethodGates(clock.clock, clock.sleep),
                                       jitter=lambda: 0.5)
        with self.assertRaises(PullError):
            client.call("team.info")
        self.assertEqual(clock.slept[:3], [0.5, 1.0, 2.0])

    def test_auth_errors_stop_immediately(self):
        clock = FakeClock()
        with self.assertRaises(SlackApiError):
            run(FakeSlack(errors={"team.info": "invalid_auth"}), clock=clock)
        self.assertEqual(clock.slept, [])

    def test_network_exceptions_become_retryable_responses(self):
        saved = pull_test.urllib.request.urlopen

        def boom(*_args, **_kwargs):
            raise ConnectionResetError("reset by peer")

        pull_test.urllib.request.urlopen = boom
        try:
            response = pull_test.urllib_exchange("GET", "https://slack.com/api/team.info", {}, None)
        finally:
            pull_test.urllib.request.urlopen = saved
        self.assertEqual(response.status, pull_test.NETWORK_ERROR)
        self.assertIn(b"ConnectionResetError", response.body)
        self.assertEqual(pull_test.retry_after_seconds(response, attempt=1), 2.0)

    def test_pacing_spaces_calls_at_the_tier_rate(self):
        clock = FakeClock()
        gates = MethodGates(clock.clock, clock.sleep, rates={"conversations.history": 50.0})
        for _ in range(3):
            gates["conversations.history"].wait()
            gates["users.list"].wait()  # not in rates: never waits
        self.assertEqual([round(s, 6) for s in clock.slept], [1.2, 1.2])

    def test_rate_modes(self):
        internal = pull_test.rate_table("internal")
        self.assertEqual(internal["conversations.history"], 50.0)
        self.assertEqual(internal["users.list"], 20.0)
        strict = pull_test.rate_table("non_marketplace")
        self.assertEqual(strict["conversations.history"], 1.0)
        self.assertEqual(strict["conversations.replies"], 1.0)
        self.assertEqual(strict["users.list"], 20.0)
        self.assertEqual(pull_test.page_limits("internal"), {})
        self.assertEqual(pull_test.page_limits("non_marketplace")["conversations.history"], 15)
        with self.assertRaises(PullError):
            pull_test.rate_table("fast")

    def test_non_marketplace_mode_uses_15_per_page_and_one_call_a_minute(self):
        fake = BigSlack(messages=40, replies_per_thread=0)
        clock = FakeClock()
        gates = MethodGates(clock.clock, clock.sleep, rates=pull_test.rate_table("non_marketplace"))
        with tempfile.TemporaryDirectory() as tmp:
            backfill(fake, Path(tmp) / "intake.jsonl", clock=clock, gates=gates, rate_mode="non_marketplace")
        history_limits = [limit for method, limit in fake.limits if method == "conversations.history"]
        self.assertEqual(history_limits, [15, 15, 15])
        self.assertGreaterEqual(clock.now, 120.0)  # three history calls spaced 60s apart

    def test_rate_mode_setting(self):
        saved = os.environ.get("SLACK_RATE_MODE")
        try:
            os.environ.pop("SLACK_RATE_MODE", None)
            self.assertEqual(pull_test.resolve_rate_mode(None), "internal")
            os.environ["SLACK_RATE_MODE"] = "non_marketplace"
            self.assertEqual(pull_test.resolve_rate_mode(None), "non_marketplace")
            self.assertEqual(pull_test.resolve_rate_mode("internal"), "internal")
            os.environ["SLACK_RATE_MODE"] = "turbo"
            with self.assertRaises(PullError):
                pull_test.resolve_rate_mode(None)
        finally:
            if saved is None:
                os.environ.pop("SLACK_RATE_MODE", None)
            else:
                os.environ["SLACK_RATE_MODE"] = saved

    def test_resume_flag_needs_output(self):
        self.assertEqual(pull_test.main(["--resume"]), 2)


if __name__ == "__main__":
    unittest.main()
