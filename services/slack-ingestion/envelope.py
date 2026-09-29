"""Wrap a raw Slack Events API delivery as EMBER-39 intake.

Stdlib only. The envelope is {"type": "slack", "body": <parsed object>}.
No cleaning, sorting, thread reconstruction, or embeddings.

Slack signs every request with the app's signing secret. The headers are
X-Slack-Request-Timestamp and X-Slack-Signature, and the signature is
v0=<hex HMAC-SHA256 of "v0:<timestamp>:<raw body>">. A timestamp more than
five minutes from now is rejected so a captured request cannot be replayed.
See Slack "Verifying requests from Slack".

A url_verification request is Slack checking the Request URL. It is
answered with its challenge and is not intake.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import time
from typing import NamedTuple

LOG = logging.getLogger("slack-ingestion")

# Slack event payloads are small. This bounds what a stub will buffer.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TIMESTAMP_SKEW = 5 * 60
SIGNATURE_VERSION = "v0"

_hmac_skip_warned = False


class WebhookError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Delivery(NamedTuple):
    """Exactly one field is set: the intake envelope, or a URL challenge."""

    envelope: dict | None
    challenge: str | None


def note_verification(secret: str) -> None:
    """Warn once when signature checks are skipped because no secret is set."""
    global _hmac_skip_warned
    if secret or _hmac_skip_warned:
        return
    LOG.warning("SLACK_SIGNING_SECRET is unset; HMAC verification skipped")
    _hmac_skip_warned = True


def expected_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = f"{SIGNATURE_VERSION}:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    """Check X-Slack-Signature against the raw body and request timestamp.

    Only the v0 scheme Slack documents today is accepted. Freshness is a
    separate check (check_request) so this function has no clock.
    """
    if not signature.startswith(SIGNATURE_VERSION + "="):
        return False
    expected = expected_signature(secret, timestamp, body)
    if len(signature) != len(expected):
        return False
    return hmac.compare_digest(expected, signature)


def check_request(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
) -> None:
    """Raise WebhookError(401) unless the request is signed and fresh."""
    if not timestamp or not signature:
        raise WebhookError(401, "missing signature")
    try:
        sent = int(timestamp)
    except ValueError as exc:
        raise WebhookError(401, "invalid timestamp") from exc
    clock = time.time() if now is None else now
    if abs(clock - sent) > MAX_TIMESTAMP_SKEW:
        raise WebhookError(401, "stale timestamp")
    if not verify_signature(secret, body, timestamp, signature):
        raise WebhookError(401, "invalid signature")


def wrap_slack(payload: dict) -> dict:
    return {"type": "slack", "body": payload}


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def delivery_key(payload: dict) -> dict[str, str | None]:
    """Workspace, event, and message identifiers, when the payload has them.

    Later routing and de-duplication can use this. It is not a filter and it
    is not added to the emitted envelope. Missing fields stay None; the
    delivery is still intake. message_changed and message_deleted carry the
    edited message's ts under event.message and event.deleted_ts; event.ts
    is the time of the edit itself.
    """
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    return {
        "team_id": _text(payload.get("team_id")),
        "enterprise_id": _text(payload.get("enterprise_id")),
        "api_app_id": _text(payload.get("api_app_id")),
        "event_id": _text(payload.get("event_id")),
        "event_type": _text(event.get("type")),
        "event_subtype": _text(event.get("subtype")),
        "channel": _text(event.get("channel")),
        "ts": _text(event.get("ts")),
        "thread_ts": _text(event.get("thread_ts")),
    }


def ingest(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> Delivery:
    """Verify the optional signature, then return the envelope or a challenge."""
    if secret:
        check_request(secret, raw_body, timestamp, signature, now=now)
    else:
        note_verification(secret)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(400, "invalid json") from exc
    if not isinstance(payload, dict):
        raise WebhookError(400, "body must be a JSON object")

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise WebhookError(400, "url_verification without a challenge")
        return Delivery(envelope=None, challenge=challenge)
    return Delivery(envelope=wrap_slack(payload), challenge=None)


def emit(envelope: dict) -> None:
    """Write one envelope as a single JSON line. No queue in this scaffold."""
    sys.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
    sys.stdout.flush()
