"""The Jira admin webhook adapter (docs/ingestion/jira-webhooks.md).

Verifies X-Hub-Signature with the webhook's secret, and reads the delivery's id, site, event name
and ordering value. It never calls the Jira API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from adapters import BadRequest, Delivery, Unauthorized

LOG = logging.getLogger("intake.jira")

MAX_DELIVERY_ID = 255  # <site>:<identifier>. The header isn't signed by itself, so cap what reaches the unique index
NO_SITE = "unknown"  # delivery_id's prefix for a body with no self URL to read the site from

# Body keys whose object carries a self URL on the site, the event's own entity first
# (jira-webhooks.md, "delivery_id"). issue comes last: on comment events it is a reduced copy.
SITE_KEYS = ("comment", "worklog", "attachment", "issueLink", "property", "version", "project", "issue", "user")


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Check X-Hub-Signature (sha256=<hex HMAC-SHA256 of the raw body>), WebSub method=signature."""
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature_header.encode())


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def jira_time(value: object) -> datetime | None:
    """An ISO time with an offset ("2026-10-13T14:05:11.123-0700") or epoch milliseconds, in UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"{value!r} has no time zone")
        return parsed.astimezone(timezone.utc)
    raise TypeError(f"{value!r} is not a time")


class JiraAdapter:
    name = "jira"

    def __init__(self, secret: str) -> None:
        if not secret.strip():
            raise ValueError("JIRA_WEBHOOK_SECRET is required: intake never stores an unsigned Jira delivery")
        if secret != secret.strip():
            # A trailing newline from a YAML block would make every signature fail
            raise ValueError("JIRA_WEBHOOK_SECRET has leading or trailing whitespace; check how it was set")
        self._secret = secret

    def handshake(self, delivery: Delivery) -> None:
        return None

    def verify(self, delivery: Delivery) -> None:
        signature = delivery.headers.get("x-hub-signature", "")
        if not verify_signature(self._secret, delivery.body, signature):
            raise Unauthorized("invalid signature")

    def site(self, delivery: Delivery) -> str | None:
        """The host of the first self URL among the body's objects, e.g. ember-capstone.atlassian.net.

        Admin webhook bodies often omit cloudId, so the site comes from the entity's own link.
        """
        for key in SITE_KEYS:
            url = _dict(delivery.json.get(key)).get("self")
            if isinstance(url, str) and url:
                try:
                    host = urlsplit(url).hostname
                except ValueError:  # a malformed URL: try the next object
                    continue
                if host:
                    return host
        return None

    def delivery_id(self, delivery: Delivery) -> str:
        """<site>:<X-Atlassian-Webhook-Identifier>. The identifier is unique per site and repeats on retries."""
        identifier = delivery.headers.get("x-atlassian-webhook-identifier")
        if not identifier:
            raise BadRequest("missing X-Atlassian-Webhook-Identifier")
        delivery_id = f"{self.site(delivery) or NO_SITE}:{identifier}"
        if len(delivery_id) > MAX_DELIVERY_ID:
            raise BadRequest("X-Atlassian-Webhook-Identifier is too long")
        retry = delivery.headers.get("x-atlassian-webhook-retry")
        if retry:  # the same identifier, so it is stored once; logged to show intake answered late or not 200
            LOG.info("jira retry %s (%s flow) of delivery=%s", retry,
                     delivery.headers.get("x-atlassian-webhook-flow", ""), delivery_id)
        return delivery_id

    def account_id(self, delivery: Delivery) -> str | None:
        return self.site(delivery)

    def event_name(self, delivery: Delivery) -> str:
        """webhookEvent as sent, e.g. jira:issue_updated or comment_created."""
        event = delivery.json.get("webhookEvent")
        if not isinstance(event, str) or not event:
            raise BadRequest("missing webhookEvent")
        return event

    def occurred_at(self, delivery: Delivery) -> datetime | None:
        """jira-webhooks.md, "Ordering value": the entity's updated, then attachment.created, then timestamp.

        Deletes, links and properties use timestamp: a deleted entity's updated is its last edit.
        """
        body = delivery.json
        event = self.event_name(delivery)
        fields: list[tuple[str, object]] = []
        if not event.endswith("_deleted"):
            if event in ("jira:issue_created", "jira:issue_updated"):
                fields.append(("issue.fields.updated", _dict(_dict(body.get("issue")).get("fields")).get("updated")))
            elif event.startswith(("comment_", "worklog_")):
                key = event.partition("_")[0]
                fields.append((f"{key}.updated", _dict(body.get(key)).get("updated")))
            elif event == "attachment_created":
                fields.append(("attachment.created", _dict(body.get("attachment")).get("created")))
        fields.append(("timestamp", body.get("timestamp")))
        for field, value in fields:
            try:
                when = jira_time(value)
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                # Try the next field. With none readable, the delivery is stored anyway, ordered by arrival
                LOG.warning("unreadable %s on %s: %s", field, event, type(exc).__name__)
                continue
            if when is not None:
                return when
        return None
