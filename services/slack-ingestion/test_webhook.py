"""A sample Slack delivery becomes a valid {type, body} envelope."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import envelope  # noqa: E402
import receiver  # noqa: E402
from receiver import RecentEvents, serve  # noqa: E402
from validate import assert_valid  # noqa: E402

FIXTURE = REPO / "packages" / "ingestion-envelope" / "fixtures" / "slack.json"
MANIFEST = Path(__file__).resolve().parent / "slack-app-manifest.json"
SECRET = "test-signing-secret"
HERE = Path(__file__).resolve().parent

# Slack "Verifying requests from Slack" worked example (a slash command
# body, so not JSON). Checks the signature scheme only.
SLACK_DOC_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
SLACK_DOC_TIMESTAMP = "1531420618"
SLACK_DOC_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow"
    b"&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner"
    b"&command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands"
    b"%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
    b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
SLACK_DOC_SIGNATURE = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


def signed(body: bytes, *, secret: str = SECRET, at: float | None = None) -> tuple[str, str]:
    timestamp = str(int(time.time() if at is None else at))
    return timestamp, envelope.expected_signature(secret, timestamp, body)


def message_event(team: str, event_id: str, text: str, **event_fields) -> dict:
    event = {"type": "message", "channel": "C0TEST", "user": "U0TEST", "text": text, "ts": "1717000001.000200"}
    event.update(event_fields)
    return {
        "token": "legacy-verification-token",
        "team_id": team,
        "api_app_id": "A0TEST",
        "type": "event_callback",
        "event_id": event_id,
        "event_time": 1717000001,
        "event": event,
    }


class EnvelopeContractTest(unittest.TestCase):
    def setUp(self):
        envelope._hmac_skip_warned = False

    def test_receiver_does_not_hard_code_a_workspace_or_channel(self):
        for name in ("envelope.py", "receiver.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertNotIn("SLACK_TEST_CHANNEL", text)
            self.assertNotIn("SLACK_BOT_TOKEN", text)
            self.assertNotIn("xoxb-", text)
            self.assertNotIn('team_id == "', text)
            self.assertNotIn('channel == "', text)

    def test_golden_fixture_body_round_trips(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw = json.dumps(fixture["body"]).encode("utf-8")
        got = envelope.ingest(raw, None, None, secret="")
        self.assertIsNone(got.challenge)
        self.assertEqual(got.envelope, fixture)
        self.assertEqual(set(got.envelope), {"type", "body"})
        key = envelope.delivery_key(got.envelope["body"])
        self.assertEqual(key["team_id"], "T012WORKSPACE")
        self.assertEqual(key["event_id"], "Ev012EVENT")
        self.assertEqual(key["event_type"], "message")
        self.assertEqual(key["channel"], "C012AB3CD")
        assert_valid(got.envelope)

    def test_keys_two_workspaces_without_filtering(self):
        deliveries = [
            message_event("T0AAA", "Ev1", "We picked Postgres."),
            message_event("T0BBB", "Ev2", "Reply in a thread.", thread_ts="1717000000.000100"),
        ]
        keys = []
        for payload in deliveries:
            got = envelope.ingest(json.dumps(payload).encode("utf-8"), None, None, "")
            self.assertEqual(got.envelope, {"type": "slack", "body": payload})
            assert_valid(got.envelope)
            keys.append(envelope.delivery_key(got.envelope["body"]))
        self.assertEqual([k["team_id"] for k in keys], ["T0AAA", "T0BBB"])
        self.assertEqual(keys[1]["thread_ts"], "1717000000.000100")

    def test_edit_and_delete_subtypes_stay_raw(self):
        edited = message_event(
            "T0AAA",
            "Ev3",
            "",
            subtype="message_changed",
            message={"type": "message", "text": "Actually MySQL.", "ts": "1717000001.000200"},
            previous_message={"type": "message", "text": "We picked Postgres.", "ts": "1717000001.000200"},
        )
        got = envelope.ingest(json.dumps(edited).encode("utf-8"), None, None, "")
        self.assertEqual(got.envelope["body"], edited)
        self.assertEqual(envelope.delivery_key(edited)["event_subtype"], "message_changed")
        assert_valid(got.envelope)

    def test_reaction_key_reads_the_channel_from_the_item(self):
        payload = message_event("T0AAA", "Ev5", "")
        payload["event"] = {
            "type": "reaction_added",
            "user": "U0TEST",
            "reaction": "raised_hands",
            "item": {"type": "message", "channel": "C0DEC", "ts": "1717000001.000200"},
        }
        got = envelope.ingest(json.dumps(payload).encode("utf-8"), None, None, "")
        self.assertEqual(got.envelope["body"], payload)
        self.assertEqual(envelope.delivery_key(payload)["channel"], "C0DEC")

    def test_non_event_callbacks_are_still_intake(self):
        raw = b'{"type":"app_rate_limited","team_id":"T0AAA","minute_rate_limited":1717000000,"api_app_id":"A0TEST"}'
        got = envelope.ingest(raw, None, None, "")
        self.assertEqual(got.envelope["body"]["type"], "app_rate_limited")
        assert_valid(got.envelope)

    def test_url_verification_is_answered_not_emitted(self):
        raw = b'{"token":"t","challenge":"3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P","type":"url_verification"}'
        got = envelope.ingest(raw, None, None, "")
        self.assertIsNone(got.envelope)
        self.assertEqual(got.challenge, "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P")

    def test_url_verification_without_challenge_is_rejected(self):
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(b'{"type":"url_verification"}', None, None, "")
        self.assertEqual(caught.exception.status, 400)

    def test_slack_documented_signature_vector(self):
        self.assertTrue(
            envelope.verify_signature(SLACK_DOC_SECRET, SLACK_DOC_BODY, SLACK_DOC_TIMESTAMP, SLACK_DOC_SIGNATURE)
        )

    def test_accepts_matching_signature(self):
        raw = json.dumps(message_event("T0AAA", "Ev4", "hi")).encode("utf-8")
        timestamp, signature = signed(raw)
        got = envelope.ingest(raw, timestamp, signature, SECRET)
        self.assertEqual(got.envelope["type"], "slack")

    def test_rejects_bad_signature(self):
        raw = b'{"type":"event_callback"}'
        timestamp, _ = signed(raw)
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(raw, timestamp, "v0=" + "ab" * 32, SECRET)
        self.assertEqual(caught.exception.status, 401)

    def test_rejects_signature_for_a_different_body(self):
        timestamp, signature = signed(b'{"type":"event_callback","a":1}')
        with self.assertRaises(envelope.WebhookError):
            envelope.ingest(b'{"type":"event_callback","a":2}', timestamp, signature, SECRET)

    def test_rejects_unknown_signature_version(self):
        raw = b'{"type":"event_callback"}'
        timestamp, signature = signed(raw)
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(raw, timestamp, "v1=" + signature[3:], SECRET)
        self.assertEqual(caught.exception.status, 401)

    def test_rejects_stale_and_future_timestamps(self):
        raw = b'{"type":"event_callback"}'
        now = 1_800_000_000
        for skew in (-301, 301):
            timestamp, signature = signed(raw, at=now + skew)
            with self.assertRaises(envelope.WebhookError) as caught:
                envelope.ingest(raw, timestamp, signature, SECRET, now=now)
            self.assertEqual(caught.exception.message, "stale timestamp")
        timestamp, signature = signed(raw, at=now - 299)
        self.assertIsNotNone(envelope.ingest(raw, timestamp, signature, SECRET, now=now).envelope)

    def test_rejects_missing_headers_when_secret_set(self):
        for timestamp, signature in ((None, "v0=x"), ("1", None), ("abc", "v0=x")):
            with self.assertRaises(envelope.WebhookError) as caught:
                envelope.ingest(b"{}", timestamp, signature, SECRET)
            self.assertEqual(caught.exception.status, 401)

    def test_skips_hmac_and_warns_when_secret_unset(self):
        with self.assertLogs("slack-ingestion", level="WARNING") as logs:
            envelope.ingest(b'{"type":"event_callback"}', None, None, "")
        self.assertTrue(any("HMAC verification skipped" in line for line in logs.output))

    def test_rejects_non_object_body(self):
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(b"[1, 2]", None, None, "")
        self.assertEqual(caught.exception.status, 400)


class ManifestTest(unittest.TestCase):
    """slack-app-manifest.json is what a new workspace installs from."""

    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_scopes_match_the_receiver(self):
        self.assertEqual(
            self.manifest["oauth_config"]["scopes"]["bot"],
            list(receiver.REQUIRED_BOT_SCOPES),
        )

    def test_events_match_the_receiver(self):
        events = self.manifest["settings"]["event_subscriptions"]
        self.assertEqual(events["bot_events"], list(receiver.RECOMMENDED_BOT_EVENTS))
        self.assertTrue(events["request_url"].endswith(receiver.WEBHOOK_PATH))

    def test_no_direct_message_access(self):
        scopes = self.manifest["oauth_config"]["scopes"]
        self.assertNotIn("user", scopes)
        for scope in scopes["bot"]:
            self.assertFalse(scope.startswith(("im:", "mpim:", "chat:")), scope)
        for event in self.manifest["settings"]["event_subscriptions"]["bot_events"]:
            self.assertFalse(event.startswith(("message.im", "message.mpim")), event)

    def test_http_events_not_socket_mode(self):
        self.assertIs(self.manifest["settings"]["socket_mode_enabled"], False)


class RecentEventsTest(unittest.TestCase):
    def test_duplicate_ids_are_seen_once_per_workspace(self):
        recent = RecentEvents(limit=2)
        self.assertTrue(recent.first_time("T1", "Ev1"))
        self.assertFalse(recent.first_time("T1", "Ev1"))
        self.assertTrue(recent.first_time("T2", "Ev1"))
        self.assertTrue(recent.first_time("T1", None))
        self.assertTrue(recent.first_time("T1", None))

    def test_oldest_id_is_forgotten_past_the_limit(self):
        recent = RecentEvents(limit=2)
        for event_id in ("Ev1", "Ev2", "Ev3"):
            recent.first_time("T1", event_id)
        self.assertTrue(recent.first_time("T1", "Ev1"))
        self.assertFalse(recent.first_time("T1", "Ev3"))


class WebhookHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prior_secret = os.environ.get("SLACK_SIGNING_SECRET")
        os.environ["SLACK_SIGNING_SECRET"] = SECRET
        cls.server = serve("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)
        cls.server.server_close()
        if cls._prior_secret is None:
            os.environ.pop("SLACK_SIGNING_SECRET", None)
        else:
            os.environ["SLACK_SIGNING_SECRET"] = cls._prior_secret

    def _post(self, body: bytes, *, sign: bool = True, path: str = "/webhook/slack", headers: dict | None = None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if sign:
            timestamp, signature = signed(body)
            request.add_header("X-Slack-Request-Timestamp", timestamp)
            request.add_header("X-Slack-Signature", signature)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_post_accepts_signed_event(self):
        body = json.dumps(message_event("T0HTTP", "EvHttp1", "Decision: ship Friday.")).encode("utf-8")
        status, payload = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "accepted"})

    def test_retry_of_emitted_event_is_acknowledged_once(self):
        body = json.dumps(message_event("T0HTTP", "EvHttpRetry", "Once only.")).encode("utf-8")
        self.assertEqual(self._post(body), (200, {"status": "accepted"}))
        status, payload = self._post(
            body, headers={"X-Slack-Retry-Num": "1", "X-Slack-Retry-Reason": "http_timeout"}
        )
        self.assertEqual((status, payload), (200, {"status": "duplicate"}))

    def test_url_verification_returns_challenge(self):
        body = b'{"token":"t","challenge":"abc123","type":"url_verification"}'
        self.assertEqual(self._post(body), (200, {"challenge": "abc123"}))

    def test_unsigned_url_verification_is_rejected_when_secret_set(self):
        body = b'{"token":"t","challenge":"abc123","type":"url_verification"}'
        status, _payload = self._post(body, sign=False)
        self.assertEqual(status, 401)

    def test_post_rejects_missing_signature(self):
        status, _payload = self._post(b'{"type":"event_callback"}', sign=False)
        self.assertEqual(status, 401)

    def test_post_rejects_invalid_json(self):
        status, payload = self._post(b"not json")
        self.assertEqual((status, payload), (400, {"error": "invalid json"}))

    def test_unknown_path_is_404(self):
        status, _payload = self._post(b"{}", path="/webhook/github")
        self.assertEqual(status, 404)

    def test_oversized_body_is_413(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook/slack",
            data=b"{}",
            method="POST",
            headers={"Content-Length": str(envelope.MAX_BODY_BYTES + 1)},
        )
        try:
            urllib.request.urlopen(request, timeout=2)
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError:
            status = 413  # server closed before reading the oversized body
        self.assertEqual(status, 413)

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
