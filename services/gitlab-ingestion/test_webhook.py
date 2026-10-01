"""A sample GitLab delivery becomes a valid {type, body} envelope."""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import envelope  # noqa: E402
import receiver  # noqa: E402
from receiver import RecentDeliveries, serve  # noqa: E402
from validate import assert_valid  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = REPO / "packages" / "ingestion-envelope" / "fixtures" / "gitlab.json"
SIGNING_TOKEN = "whsec_" + base64.b64encode(b"test-signing-key").decode("ascii")
KEY = envelope.signing_key(SIGNING_TOKEN)
SECRET_TOKEN = "test-secret-token"

# Standard Webhooks worked example. GitLab's signing token uses the same
# scheme: whsec_ key, "<id>.<timestamp>.<body>", v1,<base64 HMAC-SHA256>.
SPEC_TOKEN = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SPEC_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SPEC_TIMESTAMP = "1614265330"
SPEC_BODY = b'{"test": 2432232314}'
SPEC_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def note_hook(iid: int, text: str) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 7, "name": "Pat Example", "username": "pat"},
        "project_id": 42,
        "project": {"id": 42, "path_with_namespace": "acme/widget"},
        "object_attributes": {"id": 900 + iid, "note": text, "noteable_type": "MergeRequest", "discussion_id": "d1"},
        "merge_request": {"iid": iid, "title": "Add shared ingestion intake"},
    }


def signed_headers(body: bytes, message_id: str, *, key: bytes = KEY, at: float | None = None) -> dict[str, str]:
    timestamp = str(int(time.time() if at is None else at))
    return {
        "webhook-id": message_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": envelope.expected_signature(key, message_id, timestamp, body),
    }


def signed_ingest(body: bytes, delivery: str = "msg_1", **overrides) -> dict:
    """Ingest a correctly signed delivery, with any keyword argument replaced."""
    headers = signed_headers(body, delivery)
    kwargs = {
        "key": KEY,
        "secret_token": "",
        "message_id": headers["webhook-id"],
        "timestamp": headers["webhook-timestamp"],
        "signature": headers["webhook-signature"],
    }
    kwargs.update(overrides)
    return envelope.ingest(body, **kwargs)


class EnvelopeContractTest(unittest.TestCase):
    def setUp(self):
        envelope._verification_skip_warned = False

    def test_receiver_does_not_hard_code_a_project_or_hold_the_api_token(self):
        for name in ("envelope.py", "receiver.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertNotIn("GITLAB_API_TOKEN", text)
            self.assertNotIn("GITLAB_PROJECT", text)
            self.assertNotIn("PRIVATE-TOKEN", text)
            self.assertNotIn("urllib.request", text)
            self.assertNotIn('path_with_namespace") == "', text)

    def test_golden_fixture_body_round_trips(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw = json.dumps(fixture["body"]).encode("utf-8")
        got = envelope.ingest(raw, key=None, secret_token="")
        self.assertEqual(got, fixture)
        assert_valid(got)

    def test_spec_worked_example_signature(self):
        key = envelope.signing_key(SPEC_TOKEN)
        self.assertEqual(envelope.expected_signature(key, SPEC_ID, SPEC_TIMESTAMP, SPEC_BODY), SPEC_SIGNATURE)
        self.assertTrue(envelope.verify_signature(key, SPEC_BODY, SPEC_ID, SPEC_TIMESTAMP, SPEC_SIGNATURE))

    def test_signed_delivery_is_accepted(self):
        body = json.dumps(note_hook(7, "Decision: keep intake raw.")).encode("utf-8")
        got = signed_ingest(body)
        self.assertEqual(got, {"type": "gitlab", "body": json.loads(body)})
        assert_valid(got)

    def test_one_matching_signature_among_several_is_enough(self):
        body = b'{"object_kind":"issue"}'
        headers = signed_headers(body, "msg_2")
        many = f"v1,bm90LXRoaXMtb25l v2,ignored {headers['webhook-signature']}"
        self.assertEqual(signed_ingest(body, "msg_2", signature=many)["body"], {"object_kind": "issue"})

    def test_bad_signatures_are_rejected(self):
        body = b'{"object_kind":"issue"}'
        other_key = envelope.signing_key("whsec_" + base64.b64encode(b"another-key").decode("ascii"))
        cases = {
            "wrong key": {"signature": signed_headers(body, "msg_3", key=other_key)["webhook-signature"]},
            "missing signature": {"signature": None},
            "missing id": {"message_id": None},
            "tampered id": {"message_id": "msg_other"},
            "non-numeric timestamp": {"timestamp": "yesterday"},
            "unversioned": {"signature": signed_headers(body, "msg_3")["webhook-signature"].split(",", 1)[1]},
        }
        for name, override in cases.items():
            with self.subTest(name):
                with self.assertRaises(envelope.WebhookError) as caught:
                    signed_ingest(body, "msg_3", **override)
                self.assertEqual(caught.exception.status, 401)

    def test_tampered_body_is_rejected(self):
        body = b'{"object_kind":"issue"}'
        headers = signed_headers(body, "msg_4")
        with self.assertRaises(envelope.WebhookError) as caught:
            envelope.ingest(
                b'{"object_kind":"merge_request"}',
                key=KEY,
                secret_token="",
                message_id="msg_4",
                timestamp=headers["webhook-timestamp"],
                signature=headers["webhook-signature"],
            )
        self.assertEqual(caught.exception.status, 401)

    def test_stale_timestamp_is_rejected(self):
        body = b'{"object_kind":"issue"}'
        old = time.time() - envelope.MAX_TIMESTAMP_SKEW - 5
        headers = signed_headers(body, "msg_5", at=old)
        with self.assertRaises(envelope.WebhookError) as caught:
            signed_ingest(body, "msg_5", timestamp=headers["webhook-timestamp"], signature=headers["webhook-signature"])
        self.assertEqual(caught.exception.message, "stale timestamp")

    def test_secret_token_is_checked(self):
        body = b'{"object_kind":"issue"}'
        got = envelope.ingest(body, key=None, secret_token=SECRET_TOKEN, token=SECRET_TOKEN)
        self.assertEqual(got["body"], {"object_kind": "issue"})
        for token in (None, "", "wrong-token", SECRET_TOKEN + "x"):
            with self.subTest(token=token):
                with self.assertRaises(envelope.WebhookError) as caught:
                    envelope.ingest(body, key=None, secret_token=SECRET_TOKEN, token=token)
                self.assertEqual(caught.exception.status, 401)

    def test_both_secrets_configured_means_both_are_checked(self):
        body = b'{"object_kind":"issue"}'
        self.assertEqual(signed_ingest(body, secret_token=SECRET_TOKEN, token=SECRET_TOKEN)["body"], {"object_kind": "issue"})
        with self.assertRaises(envelope.WebhookError):
            signed_ingest(body, secret_token=SECRET_TOKEN, token="wrong-token")
        with self.assertRaises(envelope.WebhookError):
            signed_ingest(body, secret_token=SECRET_TOKEN, token=SECRET_TOKEN, signature="v1,AAAA")

    def test_no_secret_skips_verification_with_one_warning(self):
        with self.assertLogs("gitlab-ingestion", level="WARNING") as logs:
            envelope.ingest(b"{}", key=None, secret_token="")
            envelope.ingest(b"{}", key=None, secret_token="")
        self.assertEqual(len(logs.records), 1)

    def test_signing_token_must_be_whsec_base64(self):
        for token in ("plain-secret", "whsec_", "whsec_not base64!", "WHSEC_" + SIGNING_TOKEN[6:]):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    envelope.signing_key(token)

    def test_body_must_be_a_json_object(self):
        for raw in (b"not json", b"[1,2]", b'"text"', b"\xff\xfe"):
            with self.subTest(raw=raw):
                with self.assertRaises(envelope.WebhookError) as caught:
                    envelope.ingest(raw, key=None, secret_token="")
                self.assertEqual(caught.exception.status, 400)

    def test_delivery_key_reads_project_and_iid(self):
        self.assertEqual(
            envelope.delivery_key(json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]),
            {"object_kind": "merge_request", "project_id": 42, "project_path": "acme/widget", "iid": 7, "noteable_type": None},
        )
        work_item = {"object_kind": "work_item", "project_id": 42, "iid": 9}
        self.assertEqual(envelope.delivery_key(work_item)["iid"], 9)
        self.assertEqual(envelope.delivery_key(work_item)["project_id"], 42)
        self.assertEqual(envelope.delivery_key({}), dict.fromkeys(envelope.delivery_key({}), None))

    def test_recent_deliveries_is_bounded(self):
        recent = RecentDeliveries(limit=2)
        self.assertTrue(recent.first_time("a"))
        self.assertFalse(recent.first_time("a"))
        self.assertTrue(recent.first_time("b"))
        self.assertTrue(recent.first_time("c"))
        self.assertTrue(recent.first_time("a"))
        self.assertTrue(recent.first_time(None))
        self.assertTrue(recent.first_time(None))

    def test_max_body_covers_gitlab_com_payload_limit(self):
        self.assertGreaterEqual(envelope.MAX_BODY_BYTES, 25 * 1024 * 1024)


class ContainerTest(unittest.TestCase):
    def test_image_carries_receiver_and_backfill(self):
        dockerfile = (HERE / "Dockerfile").read_text(encoding="utf-8")
        for name in ("envelope.py", "receiver.py", "pull.py"):
            self.assertIn(f"services/gitlab-ingestion/{name}", dockerfile)
        self.assertIn("USER nobody", dockerfile)
        self.assertIn("EXPOSE 8083", dockerfile)

    def test_secrets_come_only_from_environment(self):
        compose = (REPO / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        for name in (
            "GITLAB_WEBHOOK_SIGNING_TOKEN",
            "GITLAB_WEBHOOK_SECRET_TOKEN",
            "GITLAB_INGESTION_PORT",
            "GITLAB_BASE_URL",
            "GITLAB_API_TOKEN",
            "GITLAB_PROJECT",
        ):
            with self.subTest(name=name):
                self.assertIn(f"{name}: \"${{{name}", compose)
                self.assertIn(name, example)

    def test_receiver_service_never_gets_the_api_token(self):
        compose = (REPO / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        start = compose.index("\n  gitlab-ingestion:\n")
        end = compose.index("\n  gitlab-backfill:\n")
        receiver_block = compose[start:end]
        self.assertIn("GITLAB_WEBHOOK_SIGNING_TOKEN", receiver_block)
        self.assertNotIn("GITLAB_API_TOKEN", receiver_block)
        backfill_block = compose[end:].split("\n\n", 1)[0]
        self.assertIn("GITLAB_API_TOKEN", backfill_block)
        self.assertNotIn("GITLAB_WEBHOOK_SIGNING_TOKEN", backfill_block)


class ReceiverHttpTest(unittest.TestCase):
    def setUp(self):
        envelope._verification_skip_warned = False
        self._saved = {name: receiver.os.environ.get(name) for name in ("GITLAB_WEBHOOK_SIGNING_TOKEN", "GITLAB_WEBHOOK_SECRET_TOKEN")}
        receiver.os.environ["GITLAB_WEBHOOK_SIGNING_TOKEN"] = SIGNING_TOKEN
        receiver.os.environ.pop("GITLAB_WEBHOOK_SECRET_TOKEN", None)
        self.stdout = io.StringIO()
        self._redirect = redirect_stdout(self.stdout)
        self._redirect.__enter__()
        with self.assertLogs("gitlab-ingestion", level="INFO"):
            self.server = serve(host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._redirect.__exit__(None, None, None)
        for name, value in self._saved.items():
            if value is None:
                receiver.os.environ.pop(name, None)
            else:
                receiver.os.environ[name] = value

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_signed(self, payload: dict, message_id: str, event: str = "Note Hook"):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Gitlab-Event": event, "Idempotency-Key": message_id}
        headers.update(signed_headers(body, message_id))
        with self.assertLogs("gitlab-ingestion", level="INFO"):
            return self.request("POST", "/webhook/gitlab", body, headers)

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.stdout.getvalue().splitlines() if line.strip()]

    def test_health(self):
        with self.assertLogs("gitlab-ingestion", level="INFO"):
            self.assertEqual(self.request("GET", "/health"), (200, {"status": "ok"}))

    def test_unknown_paths_are_404(self):
        with self.assertLogs("gitlab-ingestion", level="INFO"):
            self.assertEqual(self.request("GET", "/webhook/gitlab")[0], 404)
            self.assertEqual(self.request("POST", "/webhook/github", b"{}", {"Content-Type": "application/json"})[0], 404)

    def test_signed_delivery_is_emitted_once_and_retries_are_acknowledged(self):
        payload = note_hook(7, "Decision: keep intake raw.")
        self.assertEqual(self.post_signed(payload, "msg_a"), (200, {"status": "accepted"}))
        self.assertEqual(self.post_signed(payload, "msg_a"), (200, {"status": "duplicate"}))
        self.assertEqual(self.post_signed(note_hook(8, "Second comment."), "msg_b"), (200, {"status": "accepted"}))
        got = self.lines()
        self.assertEqual([line["body"]["merge_request"]["iid"] for line in got], [7, 8])
        for line in got:
            assert_valid(line)

    def test_unsigned_delivery_is_rejected_and_not_emitted(self):
        with self.assertLogs("gitlab-ingestion", level="INFO"):
            status, reply = self.request("POST", "/webhook/gitlab", b'{"object_kind":"issue"}', {"Content-Type": "application/json"})
        self.assertEqual((status, reply), (401, {"error": "missing signature"}))
        self.assertEqual(self.lines(), [])

    def test_oversized_body_is_refused_before_reading(self):
        with self.assertLogs("gitlab-ingestion", level="INFO"):
            status, _ = self.request(
                "POST",
                "/webhook/gitlab",
                b"{}",
                {"Content-Type": "application/json", "Content-Length": str(envelope.MAX_BODY_BYTES + 1)},
            )
        self.assertEqual(status, 413)

    def test_malformed_signing_token_stops_startup(self):
        receiver.os.environ["GITLAB_WEBHOOK_SIGNING_TOKEN"] = "not-a-whsec-token"
        with self.assertRaises(SystemExit):
            serve(host="127.0.0.1", port=0)


if __name__ == "__main__":
    unittest.main()
