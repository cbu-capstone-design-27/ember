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

    def test_gives_up_after_repeated_throttles(self):
        with self.assertRaises(PullError) as caught:
            run(FakeSlack(throttle={"team.info": 10}))
        self.assertIn("still throttled", str(caught.exception))

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

    def test_main_writes_jsonl_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "intake.jsonl"
            saved_exchange = pull_test.urllib_exchange
            saved_gates = pull_test.MethodGates
            os.environ["SLACK_BOT_TOKEN"] = TOKEN
            os.environ.pop("SLACK_TEST_CHANNEL", None)
            clock = FakeClock()
            pull_test.urllib_exchange = FakeSlack()
            pull_test.MethodGates = lambda: saved_gates(clock.clock, clock.sleep)
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


if __name__ == "__main__":
    unittest.main()
