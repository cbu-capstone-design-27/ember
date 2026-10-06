"""Wrap a raw GitLab webhook body as EMBER-39 intake.

Stdlib only. The envelope is {"type": "gitlab", "body": <parsed object>}.
No cleaning, sorting, or embeddings.

GitLab offers two ways to prove a delivery came from GitLab, both set on
the webhook itself (see GitLab "Webhooks"):

- Signing token (recommended). A whsec_-prefixed secret. The key is the
  base64-decoded remainder. GitLab sends webhook-id, webhook-timestamp and
  webhook-signature, where the signature is v1,<base64 HMAC-SHA256 of
  "<webhook-id>.<webhook-timestamp>.<raw body>">. The header may hold more
  than one space-separated signature. A timestamp more than five minutes
  from now is rejected so a captured request cannot be replayed.
- Secret token (legacy). A plain string GitLab echoes in X-Gitlab-Token.

Every secret that is configured is checked. With neither configured,
verification is skipped and a warning is logged so a local stub can run.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import sys
import threading
import time

LOG = logging.getLogger("gitlab-ingestion")

# GitLab.com's maximum webhook payload. A 413 counts as a failed delivery,
# and enough of those disable the webhook, so never reject a body GitLab can send.
MAX_BODY_BYTES = 25 * 1024 * 1024
MAX_TIMESTAMP_SKEW = 5 * 60
SIGNING_TOKEN_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"

_verification_skip_warned = False
_emit_lock = threading.Lock()


class WebhookError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def signing_key(token: str) -> bytes:
    """Decode a whsec_ signing token into its HMAC key.

    Raises ValueError for anything that is not whsec_ plus base64, so a
    mistyped token fails at startup instead of rejecting every delivery.
    """
    if not token.startswith(SIGNING_TOKEN_PREFIX):
        raise ValueError(f"signing token must start with {SIGNING_TOKEN_PREFIX}")
    try:
        key = base64.b64decode(token[len(SIGNING_TOKEN_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signing token is not valid base64 after whsec_") from exc
    if not key:
        raise ValueError("signing token is empty after whsec_")
    return key


def note_verification(key: bytes | None, secret_token: str) -> None:
    """Warn once when no webhook secret is configured."""
    global _verification_skip_warned
    if key is not None or secret_token or _verification_skip_warned:
        return
    LOG.warning(
        "GITLAB_WEBHOOK_SIGNING_TOKEN and GITLAB_WEBHOOK_SECRET_TOKEN are unset; verification skipped"
    )
    _verification_skip_warned = True


def expected_signature(key: bytes, message_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{message_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return f"{SIGNATURE_VERSION},{base64.b64encode(digest).decode('ascii')}"


def verify_signature(key: bytes, body: bytes, message_id: str, timestamp: str, header: str) -> bool:
    """True when any v1 signature in the header matches. Other versions are ignored.

    Freshness is a separate check (check_signed) so this function has no clock.
    """
    expected = expected_signature(key, message_id, timestamp, body)
    for candidate in header.split():
        if not candidate.startswith(SIGNATURE_VERSION + ","):
            continue
        if len(candidate) == len(expected) and hmac.compare_digest(expected, candidate):
            return True
    return False


def check_signed(
    key: bytes,
    body: bytes,
    message_id: str | None,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
) -> None:
    """Raise WebhookError(401) unless the delivery is signed and fresh."""
    if not message_id or not timestamp or not signature:
        raise WebhookError(401, "missing signature")
    try:
        sent = int(timestamp)
    except ValueError as exc:
        raise WebhookError(401, "invalid timestamp") from exc
    clock = time.time() if now is None else now
    if abs(clock - sent) > MAX_TIMESTAMP_SKEW:
        raise WebhookError(401, "stale timestamp")
    if not verify_signature(key, body, message_id, timestamp, signature):
        raise WebhookError(401, "invalid signature")


def check_secret_token(expected: str, received: str | None) -> None:
    """Raise WebhookError(401) unless X-Gitlab-Token matches the secret token."""
    if not received:
        raise WebhookError(401, "missing token")
    if not hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8")):
        raise WebhookError(401, "invalid token")


def wrap_gitlab(payload: dict) -> dict:
    return {"type": "gitlab", "body": payload}


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def delivery_key(payload: dict) -> dict[str, int | str | None]:
    """Hook kind, project, and object identifiers, when the payload has them.

    Later routing can use this. It is not a filter and it is not added to
    the emitted envelope. Missing fields stay None; the delivery is still
    intake. Work Item hooks carry their fields at the top level as well as
    under object_attributes, so iid falls back to the top level.
    """
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    attributes = payload.get("object_attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    return {
        "object_kind": _text(payload.get("object_kind")),
        "project_id": _int(project.get("id")) or _int(payload.get("project_id")),
        "project_path": _text(project.get("path_with_namespace")),
        "iid": _int(attributes.get("iid")) or _int(payload.get("iid")),
        "noteable_type": _text(attributes.get("noteable_type")),
    }


def ingest(
    raw_body: bytes,
    *,
    key: bytes | None,
    secret_token: str,
    message_id: str | None = None,
    timestamp: str | None = None,
    signature: str | None = None,
    token: str | None = None,
    now: float | None = None,
) -> dict:
    """Check every configured secret, then return the {type, body} envelope."""
    if key is not None:
        check_signed(key, raw_body, message_id, timestamp, signature, now=now)
    if secret_token:
        check_secret_token(secret_token, token)
    note_verification(key, secret_token)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(400, "invalid json") from exc
    if not isinstance(payload, dict):
        raise WebhookError(400, "body must be a JSON object")
    return wrap_gitlab(payload)


def emit(envelope: dict) -> None:
    """Write one envelope as a single JSON line. No queue in this scaffold.

    The receiver is threaded, so writes are serialized to keep each
    envelope on its own line.
    """
    line = json.dumps(envelope, separators=(",", ":")) + "\n"
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()
