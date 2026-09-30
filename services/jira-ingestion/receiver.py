"""Jira admin-webhook receiver. POST /webhook/jira.

Site- and project-agnostic. Stdlib HTTP server. Verifies X-Hub-Signature
when JIRA_WEBHOOK_SECRET is set, wraps the raw JSON body as
{"type":"jira","body":...}, and logs it. When the payload includes them,
logs cloudId, project key, and issue key for later routing. Does not
filter on a configured site or project.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from envelope import (
    MAX_BODY_BYTES,
    WebhookError,
    delivery_key,
    emit,
    ingest,
    note_verification,
)

LOG = logging.getLogger("jira-ingestion")
WEBHOOK_PATH = "/webhook/jira"


def _route(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


class JiraWebhookHandler(BaseHTTPRequestHandler):
    server_version = "ember-jira-ingestion"

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
        secret = os.environ.get("JIRA_WEBHOOK_SECRET", "")
        signature = self.headers.get("X-Hub-Signature")
        try:
            envelope = ingest(raw, signature, secret)
        except WebhookError as exc:
            self._send(exc.status, {"error": exc.message})
            return

        emit(envelope)
        delivery = self.headers.get("X-Atlassian-Webhook-Identifier", "")
        event = envelope["body"].get("webhookEvent")
        if not isinstance(event, str):
            event = ""
        key = delivery_key(envelope["body"])
        LOG.info(
            "ingested jira delivery=%s event=%s cloud_id=%s project_key=%s issue_key=%s",
            delivery,
            event,
            key["cloud_id"],
            key["project_key"],
            key["issue_key"],
        )
        self._send(202, {"status": "accepted"})


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    if port is None:
        port = int(os.environ.get("JIRA_INGESTION_PORT", "8081"))
    note_verification(os.environ.get("JIRA_WEBHOOK_SECRET", ""))
    server = ThreadingHTTPServer((host, port), JiraWebhookHandler)
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
