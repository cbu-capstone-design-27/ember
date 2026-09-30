"""Slack Events API receiver. POST /webhook/slack.

Workspace-agnostic. Stdlib HTTP server. Verifies X-Slack-Signature when
SLACK_SIGNING_SECRET is set, answers Slack's url_verification challenge,
wraps every other JSON body as {"type":"slack","body":...}, and logs it.
Logs team, event, and channel ids for later routing. Does not filter on a
configured workspace or channel and holds no Slack token.

Slack expects a 2xx within three seconds and retries up to three times
otherwise (X-Slack-Retry-Num). Nothing slow runs in the request path. A
retry of an event_id already emitted by this process is acknowledged and
not emitted again. That memory is lost on restart, so the pipeline must
still treat team_id + event_id as its idempotency key.

REQUIRED_BOT_SCOPES and RECOMMENDED_BOT_EVENTS are what the Slack app
must be configured with (slack-app-manifest.json). The receiver does not
read them; test_webhook.py keeps the manifest in step with them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from envelope import (
    MAX_BODY_BYTES,
    WebhookError,
    delivery_key,
    emit,
    ingest,
    note_verification,
)

LOG = logging.getLogger("slack-ingestion")
WEBHOOK_PATH = "/webhook/slack"
RECENT_EVENT_IDS = 10_000

# Bot token scopes. Channels the bot is invited to, public and private.
# No im:* or mpim:* scopes: direct messages are out of scope.
REQUIRED_BOT_SCOPES = (
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "users:read",
    "users:read.email",
    "team:read",
    "reactions:read",
)

# Configure these on the Slack app. Delivery intake does not filter on them.
# message.* also carries edits (message_changed), deletes (message_deleted),
# and thread replies (thread_ts).
RECOMMENDED_BOT_EVENTS = (
    "message.channels",
    "message.groups",
    "reaction_added",
    "reaction_removed",
    "channel_created",
    "channel_rename",
    "channel_archive",
    "channel_unarchive",
    "channel_deleted",
    "group_rename",
    "group_archive",
    "group_unarchive",
    "member_joined_channel",
    "member_left_channel",
    "user_change",
    "team_join",
    "app_uninstalled",
    "tokens_revoked",
)


def _route(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


class RecentEvents:
    """Bounded, thread-safe memory of (team_id, event_id) already emitted."""

    def __init__(self, limit: int = RECENT_EVENT_IDS) -> None:
        self._limit = limit
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._lock = threading.Lock()

    def first_time(self, team_id: str | None, event_id: str | None) -> bool:
        """True unless this event id was already seen. No id counts as new."""
        if not event_id:
            return True
        key = (team_id or "", event_id)
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            if len(self._seen) > self._limit:
                self._seen.popitem(last=False)
            return True


class SlackEventsHandler(BaseHTTPRequestHandler):
    server_version = "ember-slack-ingestion"
    recent: RecentEvents

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - " + fmt, self.address_string(), *args)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if _route(self.path) == "/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if _route(self.path) != WEBHOOK_PATH:
            self._send(404, {"error": "not found"})
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send(400, {"error": "missing content-length"})
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send(400, {"error": "invalid content-length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large"})
            return

        raw = self.rfile.read(length)
        secret = os.environ.get("SLACK_SIGNING_SECRET", "")
        try:
            delivery = ingest(
                raw,
                self.headers.get("X-Slack-Request-Timestamp"),
                self.headers.get("X-Slack-Signature"),
                secret,
            )
        except WebhookError as exc:
            self._send(exc.status, {"error": exc.message})
            return

        if delivery.challenge is not None:
            LOG.info("answered slack url_verification")
            self._send(200, {"challenge": delivery.challenge})
            return

        envelope = delivery.envelope
        key = delivery_key(envelope["body"])
        outer = envelope["body"].get("type")
        retry = self.headers.get("X-Slack-Retry-Num", "")
        reason = self.headers.get("X-Slack-Retry-Reason", "")
        if not self.recent.first_time(key["team_id"], key["event_id"]):
            LOG.info(
                "duplicate slack delivery team_id=%s event_id=%s retry=%s reason=%s; not emitted",
                key["team_id"],
                key["event_id"],
                retry,
                reason,
            )
            self._send(200, {"status": "duplicate"})
            return

        emit(envelope)
        LOG.info(
            "ingested slack type=%s event=%s subtype=%s team_id=%s event_id=%s channel=%s retry=%s",
            outer if isinstance(outer, str) else "",
            key["event_type"],
            key["event_subtype"],
            key["team_id"],
            key["event_id"],
            key["channel"],
            retry,
        )
        self._send(200, {"status": "accepted"})


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    if port is None:
        port = int(os.environ.get("SLACK_INGESTION_PORT", "8082"))
    note_verification(os.environ.get("SLACK_SIGNING_SECRET", ""))
    handler = type("BoundSlackEventsHandler", (SlackEventsHandler,), {"recent": RecentEvents()})
    server = ThreadingHTTPServer((host, port), handler)
    LOG.info("listening on %s:%s", host, server.server_address[1])
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    server = serve()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
