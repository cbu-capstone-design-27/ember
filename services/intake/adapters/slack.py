"""The Slack Events API adapter (docs/ingestion/slack-webhooks.md).

Verifies X-Slack-Signature with the app's signing secret, answers the Request URL check, and reads
the delivery's id, workspace, event name and ordering value. It never calls the Slack API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse, Response

from adapters import BadRequest, Delivery, Unauthorized

LOG = logging.getLogger("intake.slack")

SIGNATURE_VERSION = "v0"  # the only scheme Slack documents
MAX_TIMESTAMP_SKEW = 5 * 60  # Slack's replay window, in seconds
MAX_TIMESTAMP_DIGITS = 12  # X-Slack-Request-Timestamp is epoch seconds; anything longer is stale anyway
MAX_DELIVERY_ID = 255  # event_id is short. A body is signed but its fields aren't checked, so cap what reaches the unique index


def verify_signature(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    """Check X-Slack-Signature (v0=<hex HMAC-SHA256 of "v0:<timestamp>:<raw body>">)."""
    base = f"{SIGNATURE_VERSION}:{timestamp}:".encode() + body
    expected = f"{SIGNATURE_VERSION}=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def slack_time(value: object) -> datetime | None:
    """A Slack ts ("1717000001.000200") or epoch seconds, as an exact UTC datetime. None if absent."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        whole, _, fraction = value.partition(".")
        if not whole.isdigit() or (fraction and not fraction.isdigit()):
            raise ValueError(f"{value!r} is not a Slack ts")
        micros = int((fraction + "000000")[:6])  # a ts has 6 digits; float() would round them
        return datetime.fromtimestamp(int(whole), timezone.utc) + timedelta(microseconds=micros)
    raise TypeError(f"{value!r} is not a time")


class SlackAdapter:
    name = "slack"

    def __init__(self, secret: str, clock: Callable[[], float] = time.time) -> None:
        if not secret.strip():
            raise ValueError("SLACK_SIGNING_SECRET is required: intake never stores an unsigned Slack delivery")
        if secret != secret.strip():
            # A trailing newline from a YAML block would make every signature fail
            raise ValueError("SLACK_SIGNING_SECRET has leading or trailing whitespace; check how it was set")
        self._secret = secret
        self._clock = clock

    def handshake(self, delivery: Delivery) -> Response | None:
        """Answer the Request URL check (url_verification). Slack signs it like any delivery."""
        self.verify(delivery)  # before the body is parsed, so nothing unsigned is read
        if delivery.json.get("type") != "url_verification":
            return None
        challenge = _str(delivery.json.get("challenge"))
        if challenge is None:
            raise BadRequest("url_verification without a challenge")
        return JSONResponse({"challenge": challenge})

    def verify(self, delivery: Delivery) -> None:
        timestamp = delivery.headers.get("x-slack-request-timestamp", "")
        signature = delivery.headers.get("x-slack-signature", "")
        if not timestamp or not signature:
            raise Unauthorized("missing signature")
        # isdigit alone accepts "²" (a latin-1 header byte), which int() rejects. Epoch seconds are 10 digits
        if not (timestamp.isascii() and timestamp.isdigit()) or len(timestamp) > MAX_TIMESTAMP_DIGITS:
            raise Unauthorized("invalid timestamp")
        # A captured request can't be replayed later: Slack's own window, either side of now
        if abs(self._clock() - int(timestamp)) > MAX_TIMESTAMP_SKEW:
            raise Unauthorized("stale timestamp")
        if not verify_signature(self._secret, delivery.body, timestamp, signature):
            raise Unauthorized("invalid signature")

    def delivery_id(self, delivery: Delivery) -> str:
        """event_id, which a retry repeats, or rate_limited:<team>:<minute> for the throttle notice."""
        body = delivery.json
        kind = body.get("type")
        if kind == "event_callback":
            delivery_id = _str(body.get("event_id"))
            if delivery_id is None:
                raise BadRequest("missing event_id")
        elif kind == "app_rate_limited":
            team, minute = _str(body.get("team_id")), body.get("minute_rate_limited")
            if team is None or not isinstance(minute, int) or isinstance(minute, bool):
                raise BadRequest("app_rate_limited without team_id and minute_rate_limited")
            delivery_id = f"rate_limited:{team}:{minute}"  # it has no event_id; one notice per minute
        else:
            raise BadRequest("not an Events API delivery")
        if len(delivery_id) > MAX_DELIVERY_ID:
            raise BadRequest("event_id is too long")
        retry = delivery.headers.get("x-slack-retry-num")
        if retry:  # the same event_id, so it is stored once; logged to show intake answered late
            LOG.info("slack retry %s (%s) of delivery=%s", retry,
                     delivery.headers.get("x-slack-retry-reason", ""), delivery_id)
        return delivery_id

    def account_id(self, delivery: Delivery) -> str | None:
        return _str(delivery.json.get("team_id"))

    def event_name(self, delivery: Delivery) -> str:
        """The inner event.type, plus :<subtype> when there is one, e.g. message:message_deleted."""
        body = delivery.json
        if body.get("type") != "event_callback":
            return _str(body.get("type")) or "unknown"
        event = _dict(body.get("event"))
        name = _str(event.get("type")) or "event_callback"
        subtype = _str(event.get("subtype"))
        return f"{name}:{subtype}" if subtype else name

    def occurred_at(self, delivery: Delivery) -> datetime | None:
        """slack-webhooks.md, "Ordering value": event.event_ts, then event.ts, then event_time.

        The throttle notice uses minute_rate_limited.
        """
        body = delivery.json
        if body.get("type") == "app_rate_limited":
            fields = (("minute_rate_limited", body.get("minute_rate_limited")),)
        else:
            event = _dict(body.get("event"))
            fields = (("event.event_ts", event.get("event_ts")), ("event.ts", event.get("ts")),
                      ("event_time", body.get("event_time")))
        for field, value in fields:
            try:
                when = slack_time(value)
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                # Try the next field. With none readable, the delivery is stored anyway, ordered by arrival
                LOG.warning("unreadable %s on event_id=%s: %s", field, _str(body.get("event_id")), type(exc).__name__)
                continue
            if when is not None:
                return when
        return None
