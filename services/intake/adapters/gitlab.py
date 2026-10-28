"""The GitLab project webhook adapter (docs/ingestion/gitlab-webhooks.md).

Verifies the signing token (webhook-signature) and the legacy secret token (X-Gitlab-Token), each when
it is configured, and reads the delivery's id, account, event name and ordering value. It never calls
the GitLab API. The checks are services/gitlab-ingestion/envelope.py's, which this replaces on the cluster.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from adapters import BadRequest, Delivery, Unauthorized

LOG = logging.getLogger("intake.gitlab")

MAX_DELIVERY_ID = 255  # webhook-id isn't signed by the legacy token, so cap what reaches the unique index
MAX_TIMESTAMP_SKEW = 5 * 60  # seconds either side of webhook-timestamp, so a captured request can't be replayed
SIGNING_TOKEN_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"
# gitlab-webhooks.md, "Ordering value": GitLab doesn't say these bump updated_at, so they order by arrival
APPROVALS = ("approval", "unapproval", "approved", "unapproved")
UPDATED_AT_KINDS = ("merge_request", "issue", "work_item", "note")


def signing_key(token: str) -> bytes:
    """The HMAC key in a whsec_ signing token: the base64 after the prefix."""
    if not token.startswith(SIGNING_TOKEN_PREFIX):
        raise ValueError(f"GITLAB_WEBHOOK_SIGNING_TOKEN must start with {SIGNING_TOKEN_PREFIX}")
    try:
        key = base64.b64decode(token[len(SIGNING_TOKEN_PREFIX):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"GITLAB_WEBHOOK_SIGNING_TOKEN is not base64 after {SIGNING_TOKEN_PREFIX}") from exc
    if not key:
        raise ValueError(f"GITLAB_WEBHOOK_SIGNING_TOKEN is empty after {SIGNING_TOKEN_PREFIX}")
    return key


def verify_signature(key: bytes, body: bytes, message_id: str, timestamp: str, header: str) -> bool:
    """True when any v1 signature in webhook-signature is the HMAC-SHA256 of "<id>.<timestamp>.<body>".

    The header may hold several space-separated signatures; other versions are ignored.
    """
    digest = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    expected = f"{SIGNATURE_VERSION},{base64.b64encode(digest).decode('ascii')}".encode()
    return any(hmac.compare_digest(expected, candidate.encode())
               for candidate in header.split() if candidate.startswith(SIGNATURE_VERSION + ","))


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def gitlab_time(value: object) -> datetime | None:
    """ISO 8601 (REST, and webhooks since GitLab 19.0) or the older "2026-01-16 05:56:25 UTC", in UTC."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TypeError(f"{value!r} is not a time")
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone")
    return parsed.astimezone(timezone.utc)


def _token(name: str, value: str) -> str:
    if value != value.strip():
        # A trailing newline from a YAML block would make every delivery fail
        raise ValueError(f"{name} has leading or trailing whitespace; check how it was set")
    return value


class GitLabAdapter:
    name = "gitlab"

    def __init__(self, signing_token: str = "", secret_token: str = "", *, clock=time.time) -> None:
        if not signing_token.strip() and not secret_token.strip():
            raise ValueError("GITLAB_WEBHOOK_SIGNING_TOKEN or GITLAB_WEBHOOK_SECRET_TOKEN is required:"
                             " intake never stores an unsigned GitLab delivery")
        self._key = signing_key(_token("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)) if signing_token.strip() else None
        self._secret_token = _token("GITLAB_WEBHOOK_SECRET_TOKEN", secret_token) if secret_token.strip() else ""
        self._clock = clock

    def handshake(self, delivery: Delivery) -> None:
        return None

    def verify(self, delivery: Delivery) -> None:
        """Every configured check must pass, as the webhook may carry both."""
        if self._key is not None:
            message_id = delivery.headers.get("webhook-id", "")
            timestamp = delivery.headers.get("webhook-timestamp", "")
            signature = delivery.headers.get("webhook-signature", "")
            if not message_id or not timestamp or not signature:
                raise Unauthorized("missing signature")
            try:
                sent = int(timestamp)
            except ValueError:
                raise Unauthorized("invalid timestamp") from None
            if abs(self._clock() - sent) > MAX_TIMESTAMP_SKEW:
                raise Unauthorized("stale timestamp")
            if not verify_signature(self._key, delivery.body, message_id, timestamp, signature):
                raise Unauthorized("invalid signature")
        if self._secret_token:
            token = delivery.headers.get("x-gitlab-token", "")
            if not hmac.compare_digest(self._secret_token.encode(), token.encode()):
                raise Unauthorized("invalid token")

    def delivery_id(self, delivery: Delivery) -> str:
        """webhook-id, else Idempotency-Key: the same value, kept across GitLab's retries and a manual Resend."""
        delivery_id = delivery.headers.get("webhook-id") or delivery.headers.get("idempotency-key")
        if not delivery_id:
            raise BadRequest("missing webhook-id and Idempotency-Key")
        if len(delivery_id) > MAX_DELIVERY_ID:
            raise BadRequest("webhook-id is too long")
        return delivery_id

    def account_id(self, delivery: Delivery) -> str | None:
        """<instance host>/<top-level group>, e.g. gitlab.com/gitlab-org: one read token per tenant group.

        The host comes from X-Gitlab-Instance (GitLab sends the instance URL), the group from
        project.path_with_namespace. Without either, the delivery is stored with no account.
        """
        instance = delivery.headers.get("x-gitlab-instance", "").strip()
        try:
            host = urlsplit(instance).hostname if "://" in instance else instance.lower()
        except ValueError:  # a malformed URL
            host = None
        path = _dict(delivery.json.get("project")).get("path_with_namespace")
        group = path.split("/", 1)[0] if isinstance(path, str) else ""
        return f"{host}/{group}" if host and group else None

    def event_name(self, delivery: Delivery) -> str:
        """object_kind, plus :<action> when there is one, e.g. merge_request:update, work_item:close, push.

        The body decides, not X-Gitlab-Event: Issue Hook covers both issues and work items.
        """
        kind = delivery.json.get("object_kind")
        if not isinstance(kind, str) or not kind:
            raise BadRequest("missing object_kind")
        action = _dict(delivery.json.get("object_attributes")).get("action")
        return f"{kind}:{action}" if isinstance(action, str) and action else kind

    def occurred_at(self, delivery: Delivery) -> datetime | None:
        """gitlab-webhooks.md, "Ordering value": object_attributes.updated_at, else received_at.

        Pushes (branch deletes included) and the four approval actions always get received_at: a push
        carries only commit times, which the author controls, and approvals may not bump updated_at.
        """
        kind = delivery.json.get("object_kind")
        attributes = _dict(delivery.json.get("object_attributes"))
        if kind not in UPDATED_AT_KINDS or attributes.get("action") in APPROVALS:
            return None
        try:
            return gitlab_time(attributes.get("updated_at"))
        except (TypeError, ValueError) as exc:
            # Stored anyway, ordered by arrival
            LOG.warning("unreadable object_attributes.updated_at on %s: %s", self.event_name(delivery), type(exc).__name__)
            return None
