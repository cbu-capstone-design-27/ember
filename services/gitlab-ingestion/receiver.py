"""GitLab webhook receiver. POST /webhook/gitlab.

Project- and group-agnostic. Stdlib HTTP server. Checks the signing token
(GITLAB_WEBHOOK_SIGNING_TOKEN) and the legacy secret token
(GITLAB_WEBHOOK_SECRET_TOKEN) when they are set, wraps the raw JSON body as
{"type":"gitlab","body":...}, and logs it. Logs the hook kind and project
for later routing. Does not filter on X-Gitlab-Event, project, or group,
holds no API token, and never calls GitLab. GitLab webhooks carry the full
object, so nothing needs fetching on this path.

GitLab expects a fast 200 or 201 (10 seconds on GitLab.com). Four failures
in a row disable the webhook for a while and forty disable it for good, so
nothing slow runs in the request path. webhook-id (equal to Idempotency-Key)
is stable across retries; a delivery already emitted by this process is
acknowledged and not emitted again. That memory is lost on restart, so the
pipeline must still upsert on stable object ids.

RECOMMENDED_WEBHOOK_TRIGGERS is what to enable on the project or group
webhook. The receiver does not read it.
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
    signing_key,
)

LOG = logging.getLogger("gitlab-ingestion")
WEBHOOK_PATH = "/webhook/gitlab"
RECENT_DELIVERY_IDS = 10_000

# Webhook triggers for the first cut (docs/ingestion/gitlab.md). Confidential
# issues and confidential comments are separate triggers and stay off.
RECOMMENDED_WEBHOOK_TRIGGERS = (
    "Merge request events",
    "Issues events",
    "Comments",
)


def _route(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


class RecentDeliveries:
    """Bounded, thread-safe memory of webhook ids already emitted."""

    def __init__(self, limit: int = RECENT_DELIVERY_IDS) -> None:
        self._limit = limit
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def first_time(self, delivery_id: str | None) -> bool:
        """True unless this id was already seen. No id counts as new."""
        if not delivery_id:
            return True
        with self._lock:
            if delivery_id in self._seen:
                self._seen.move_to_end(delivery_id)
                return False
            self._seen[delivery_id] = None
            if len(self._seen) > self._limit:
                self._seen.popitem(last=False)
            return True


class GitlabWebhookHandler(BaseHTTPRequestHandler):
    server_version = "ember-gitlab-ingestion"
    recent: RecentDeliveries
    key: bytes | None
    secret_token: str

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
        delivery_id = self.headers.get("webhook-id") or self.headers.get("Idempotency-Key")
        try:
            envelope = ingest(
                raw,
                key=self.key,
                secret_token=self.secret_token,
                message_id=self.headers.get("webhook-id"),
                timestamp=self.headers.get("webhook-timestamp"),
                signature=self.headers.get("webhook-signature"),
                token=self.headers.get("X-Gitlab-Token"),
            )
        except WebhookError as exc:
            self._send(exc.status, {"error": exc.message})
            return

        event = self.headers.get("X-Gitlab-Event", "")
        if not self.recent.first_time(delivery_id):
            LOG.info("duplicate gitlab delivery id=%s event=%s; not emitted", delivery_id, event)
            self._send(200, {"status": "duplicate"})
            return

        emit(envelope)
        key = delivery_key(envelope["body"])
        LOG.info(
            "ingested gitlab delivery=%s event=%s event_uuid=%s instance=%s kind=%s "
            "project_id=%s project=%s iid=%s",
            delivery_id or "",
            event,
            self.headers.get("X-Gitlab-Event-UUID", ""),
            self.headers.get("X-Gitlab-Instance", ""),
            key["object_kind"],
            key["project_id"],
            key["project_path"],
            key["iid"],
        )
        self._send(200, {"status": "accepted"})


def load_secrets() -> tuple[bytes | None, str]:
    """Read and decode the webhook secrets once. A malformed signing token exits."""
    raw_signing = os.environ.get("GITLAB_WEBHOOK_SIGNING_TOKEN", "").strip()
    secret_token = os.environ.get("GITLAB_WEBHOOK_SECRET_TOKEN", "")
    key = None
    if raw_signing:
        try:
            key = signing_key(raw_signing)
        except ValueError as exc:
            raise SystemExit(f"GITLAB_WEBHOOK_SIGNING_TOKEN: {exc}") from exc
    return key, secret_token


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    if port is None:
        port = int(os.environ.get("GITLAB_INGESTION_PORT", "8083"))
    key, secret_token = load_secrets()
    note_verification(key, secret_token)
    handler = type(
        "BoundGitlabWebhookHandler",
        (GitlabWebhookHandler,),
        {"recent": RecentDeliveries(), "key": key, "secret_token": secret_token},
    )
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
