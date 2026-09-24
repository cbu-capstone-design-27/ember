"""A sample GitHub webhook becomes a valid {type, body} envelope."""

from __future__ import annotations

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

FIXTURE = REPO / "packages" / "ingestion-envelope" / "fixtures" / "github.json"
SECRET = "test-webhook-secret"


def sign(secret: str, body: bytes) -> str:
    import hashlib
    import hmac

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


class EnvelopeContractTest(unittest.TestCase):
    def setUp(self):
        envelope._hmac_skip_warned = False

    def test_golden_fixture_body_round_trips(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw = json.dumps(fixture["body"]).encode("utf-8")
        got = envelope.ingest(raw, signature_header=None, secret="")
        self.assertEqual(got["type"], "github")
        self.assertEqual(got["body"], fixture["body"])
        self.assertEqual(set(got), {"type", "body"})
        assert_valid(got)

    def test_minimal_payload_validates(self):
        raw = b'{"zen":"design for failure","hook_id":1}'
        got = envelope.ingest(raw, signature_header=None, secret="")
        self.assertEqual(
            got,
            {"type": "github", "body": {"zen": "design for failure", "hook_id": 1}},
        )
        assert_valid(got)

    def test_hmac_accepts_matching_signature(self):
        raw = b'{"action":"opened"}'
        got = envelope.ingest(raw, sign(SECRET, raw), SECRET)
        self.assertEqual(got["body"], {"action": "opened"})
        assert_valid(got)

    def test_hmac_rejects_bad_signature(self):
        raw = b'{"action":"opened"}'
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(raw, "sha256=" + "ab" * 32, SECRET)
        self.assertEqual(caught.exception.status, 401)

    def test_skips_hmac_and_warns_when_secret_unset(self):
        raw = b'{"action":"opened"}'
        with self.assertLogs("github-ingestion", level="WARNING") as logs:
            envelope.ingest(raw, signature_header=None, secret="")
        self.assertTrue(any("HMAC verification skipped" in line for line in logs.output))

    def test_rejects_non_object_body(self):
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(b"[1, 2]", signature_header=None, secret="")
        self.assertEqual(caught.exception.status, 400)


class WebhookHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prior_secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
        os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET
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
            os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        else:
            os.environ["GITHUB_WEBHOOK_SECRET"] = cls._prior_secret

    def _post(self, body: bytes, signature: str | None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook/github",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if signature is not None:
            request.add_header("X-Hub-Signature-256", signature)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_post_accepts_signed_payload(self):
        body = b'{"zen":"keep it logically awesome"}'
        status, payload = self._post(body, sign(SECRET, body))
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(payload), {"status": "accepted"})

    def test_post_rejects_missing_signature(self):
        status, _payload = self._post(b'{"zen":"nope"}', None)
        self.assertEqual(status, 401)

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
