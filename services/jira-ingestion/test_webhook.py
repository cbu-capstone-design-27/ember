"""A sample Jira webhook becomes a valid {type, body} envelope."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import envelope  # noqa: E402
from receiver import serve  # noqa: E402
from validate import assert_valid  # noqa: E402

FIXTURE = REPO / "packages" / "ingestion-envelope" / "fixtures" / "jira.json"
SECRET = "test-webhook-secret"
HERE = Path(__file__).resolve().parent

# Atlassian "Secure admin webhooks" published vector (payload is not JSON).
ATLASSIAN_SECRET = "It's a Secret to Everybody"
ATLASSIAN_PAYLOAD = b"Hello World!"
ATLASSIAN_SIGNATURE = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


class EnvelopeContractTest(unittest.TestCase):
    def setUp(self):
        envelope._hmac_skip_warned = False

    def test_receiver_does_not_hard_code_a_site_or_project(self):
        for name in ("envelope.py", "receiver.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertNotIn("JIRA_TEST_PROJECT", text)
            self.assertNotIn("DEFAULT_PROJECT", text)
            self.assertNotIn("ember-capstone", text)
            self.assertNotIn("atlassian.net", text)
            self.assertNotIn('project = "', text)

    def test_golden_fixture_body_round_trips(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw = json.dumps(fixture["body"]).encode("utf-8")
        got = envelope.ingest(raw, signature_header=None, secret="")
        self.assertEqual(got["type"], "jira")
        self.assertEqual(got["body"], fixture["body"])
        self.assertEqual(set(got), {"type", "body"})
        self.assertEqual(
            envelope.delivery_key(got["body"]),
            {"cloud_id": None, "project_key": "EMBER", "issue_key": "EMBER-39"},
        )
        assert_valid(got)

    def test_keys_two_sites_without_filtering(self):
        deliveries = [
            {
                "webhookEvent": "jira:issue_created",
                "cloudId": "site-a",
                "issue": {
                    "key": "EMBER-1",
                    "fields": {"project": {"key": "EMBER"}, "description": "Kept as-is."},
                },
            },
            {
                "webhookEvent": "jira:issue_updated",
                "cloudId": "site-b",
                "issue": {
                    "key": "OTHER-9",
                    "fields": {"project": {"key": "OTHER"}, "description": None},
                },
            },
        ]
        os.environ["JIRA_TEST_PROJECT"] = "EMBER"
        os.environ["JIRA_BASE_URL"] = "https://example.atlassian.net"
        try:
            keys = []
            for payload in deliveries:
                got = envelope.ingest(json.dumps(payload).encode("utf-8"), None, "")
                self.assertEqual(set(got), {"type", "body"})
                self.assertEqual(got["body"], payload)
                self.assertIn("description", got["body"]["issue"]["fields"])
                assert_valid(got)
                keys.append(envelope.delivery_key(got["body"]))
        finally:
            os.environ.pop("JIRA_TEST_PROJECT", None)
            os.environ.pop("JIRA_BASE_URL", None)
        self.assertEqual(
            keys,
            [
                {"cloud_id": "site-a", "project_key": "EMBER", "issue_key": "EMBER-1"},
                {"cloud_id": "site-b", "project_key": "OTHER", "issue_key": "OTHER-9"},
            ],
        )

    def test_project_event_without_issue_still_ingests(self):
        raw = b'{"webhookEvent":"project_created","project":{"key":"WIDGET"}}'
        got = envelope.ingest(raw, signature_header=None, secret="")
        self.assertEqual(
            envelope.delivery_key(got["body"]),
            {"cloud_id": None, "project_key": "WIDGET", "issue_key": None},
        )
        assert_valid(got)

    def test_minimal_payload_validates(self):
        raw = b'{"webhookEvent":"jira:issue_updated"}'
        got = envelope.ingest(raw, signature_header=None, secret="")
        self.assertEqual(got, {"type": "jira", "body": {"webhookEvent": "jira:issue_updated"}})
        assert_valid(got)

    def test_atlassian_published_hmac_vector(self):
        self.assertTrue(
            envelope.verify_signature(ATLASSIAN_SECRET, ATLASSIAN_PAYLOAD, ATLASSIAN_SIGNATURE)
        )

    def test_hmac_accepts_matching_signature(self):
        raw = b'{"webhookEvent":"jira:issue_created"}'
        got = envelope.ingest(raw, sign(SECRET, raw), SECRET)
        self.assertEqual(got["body"], {"webhookEvent": "jira:issue_created"})
        assert_valid(got)

    def test_hmac_rejects_bad_signature(self):
        raw = b'{"webhookEvent":"jira:issue_created"}'
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(raw, "sha256=" + "ab" * 32, SECRET)
        self.assertEqual(caught.exception.status, 401)

    def test_hmac_rejects_non_sha256_method(self):
        raw = b'{"webhookEvent":"jira:issue_created"}'
        digest = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(raw, "sha512=" + digest, SECRET)
        self.assertEqual(caught.exception.status, 401)

    def test_skips_hmac_and_warns_when_secret_unset(self):
        raw = b'{"webhookEvent":"jira:issue_created"}'
        with self.assertLogs("jira-ingestion", level="WARNING") as logs:
            envelope.ingest(raw, signature_header=None, secret="")
        self.assertTrue(any("HMAC verification skipped" in line for line in logs.output))

    def test_rejects_non_object_body(self):
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(b"[1, 2]", signature_header=None, secret="")
        self.assertEqual(caught.exception.status, 400)


class WebhookHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prior_secret = os.environ.get("JIRA_WEBHOOK_SECRET")
        os.environ["JIRA_WEBHOOK_SECRET"] = SECRET
        os.environ["JIRA_TEST_PROJECT"] = "NOT-A-FILTER"
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
            os.environ.pop("JIRA_WEBHOOK_SECRET", None)
        else:
            os.environ["JIRA_WEBHOOK_SECRET"] = cls._prior_secret
        os.environ.pop("JIRA_TEST_PROJECT", None)

    def _post(self, body: bytes, signature: str | None, query: str = ""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook/jira{query}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if signature is not None:
            request.add_header("X-Hub-Signature", signature)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_post_accepts_signed_payload(self):
        body = (
            b'{"webhookEvent":"jira:issue_created","cloudId":"site-a",'
            b'"issue":{"key":"WIDGET-3","fields":{"summary":"Parser",'
            b'"description":"What this changes.","project":{"key":"WIDGET"}}}}'
        )
        status, payload = self._post(body, sign(SECRET, body))
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(payload), {"status": "accepted"})

    def test_post_rejects_missing_signature(self):
        status, _payload = self._post(b'{"webhookEvent":"jira:issue_created"}', None)
        self.assertEqual(status, 401)

    def test_query_secret_does_not_authenticate(self):
        status, _payload = self._post(
            b'{"webhookEvent":"jira:issue_created"}',
            None,
            query="?secret=" + SECRET,
        )
        self.assertEqual(status, 401)

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
