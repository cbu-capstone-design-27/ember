"""The GitHub App adapter (docs/ingestion/github-webhooks.md).

Verifies X-Hub-Signature-256 with the App's webhook secret, and reads the delivery's id, account,
event name and ordering value. It never calls the GitHub API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from adapters import BadRequest, Delivery, Unauthorized

LOG = logging.getLogger("intake.github")

# The App subscription list (github-webhooks.md, "Event types"). It lets a live delivery carry the
# same entity types as the REST backfill, plus a few REST cannot list (discussions) or the
# backfill does not walk (checks, Actions, deployments). Configure it on the App; intake does
# not filter on it.
RECOMMENDED_WEBHOOK_EVENTS = (
    "installation",
    "installation_repositories",
    "repository",
    "push",
    "create",
    "delete",
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "commit_comment",
    "release",
    "milestone",
    "label",
    "status",
    "check_run",
    "check_suite",
    "workflow_run",
    "workflow_job",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
)

# event: (payload key of the object, its time fields, first present wins). github-webhooks.md,
# "Ordering value". Every other event, and every deleted or dismissed action, falls back to
# received_at: a deleted object's updated_at is its last edit, not the delete.
ORDERING_FIELDS = {
    "issues": ("issue", ("updated_at",)),
    "issue_comment": ("comment", ("updated_at",)),
    "pull_request": ("pull_request", ("updated_at",)),
    "pull_request_review_comment": ("comment", ("updated_at",)),
    "commit_comment": ("comment", ("updated_at",)),
    "milestone": ("milestone", ("updated_at",)),
    "pull_request_review": ("review", ("submitted_at",)),
    "release": ("release", ("published_at", "created_at")),
    "push": ("repository", ("pushed_at",)),  # epoch seconds in push payloads
}


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Check X-Hub-Signature-256 (sha256=<hex HMAC-SHA256 of the raw body>)."""
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature_header.encode())


def _time(value: object) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"{value!r} has no time zone")
        return parsed
    raise TypeError(f"{value!r} is not a time")


class GitHubAdapter:
    name = "github"

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("GITHUB_WEBHOOK_SECRET is required: intake never stores an unsigned GitHub delivery")
        self._secret = secret

    def handshake(self, delivery: Delivery) -> None:
        return None

    def verify(self, delivery: Delivery) -> None:
        signature = delivery.headers.get("x-hub-signature-256", "")
        if not verify_signature(self._secret, delivery.body, signature):
            raise Unauthorized("invalid signature")

    def delivery_id(self, delivery: Delivery) -> str:
        # A manual or API redelivery reuses it, so a redelivery of something stored is a no-op
        delivery_id = delivery.headers.get("x-github-delivery")
        if not delivery_id:
            raise BadRequest("missing X-GitHub-Delivery")
        return delivery_id

    def account_id(self, delivery: Delivery) -> str | None:
        installation = delivery.json.get("installation")
        installation_id = installation.get("id") if isinstance(installation, dict) else None
        if isinstance(installation_id, int) and not isinstance(installation_id, bool):
            return str(installation_id)
        return None

    def event_name(self, delivery: Delivery) -> str:
        """X-GitHub-Event, plus :<action> when the payload has one, e.g. pull_request:synchronize."""
        event = delivery.headers.get("x-github-event")
        if not event:
            raise BadRequest("missing X-GitHub-Event")
        action = delivery.json.get("action")
        return f"{event}:{action}" if isinstance(action, str) and action else event

    def occurred_at(self, delivery: Delivery) -> datetime | None:
        event, _, action = self.event_name(delivery).partition(":")
        if action in ("deleted", "dismissed") or event not in ORDERING_FIELDS:
            return None
        key, fields = ORDERING_FIELDS[event]
        obj = delivery.json.get(key)
        if not isinstance(obj, dict):
            return None
        for field in fields:
            if obj.get(field) is not None:
                try:
                    return _time(obj[field])
                except (TypeError, ValueError, OverflowError) as exc:
                    # Store the delivery anyway, ordered by arrival, rather than lose it
                    LOG.warning("unreadable %s.%s on %s: %s", key, field, self.delivery_id(delivery), exc)
                    return None
        return None
