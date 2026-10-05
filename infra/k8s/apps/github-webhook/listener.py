"""Placeholder GitHub App webhook listener. POST /webhook/github.

Stands in for the real worker (EMBER-56, services/github-ingestion) behind
Service github-webhook, so the route from GitHub through Funnel and nginx is
proven before that worker is deployed. Stdlib HTTP server, mounted from a
ConfigMap. Reads and discards the body, logs one line per delivery and
answers 200. Does not verify X-Hub-Signature-256, and never logs or stores
the payload.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("github-webhook-placeholder")
WEBHOOK_PATH = "/webhook/github"
# Same cap as nginx (client_max_body_size) and services/github-ingestion/envelope.py.
MAX_BODY_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 65536
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _route(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


def _clean(value: str | None) -> str:
    # Anyone can send these headers, so only a short plain token reaches the log.
    return _UNSAFE.sub("_", (value or "")[:64]) or "-"


class PlaceholderHandler(BaseHTTPRequestHandler):
    server_version = "ember-github-webhook-placeholder"
    # Socket timeout, so a stalled client cannot hold a thread forever.
    timeout = 15
    # Errors http.server answers by itself (501, malformed request) stay JSON too.
    error_content_type = "application/json"
    error_message_format = '{"error": "http %(code)d"}'

    def log_message(self, fmt: str, *args) -> None:
        # The default logs the raw request line, query string included, for every
        # request and probe. Deliveries are logged in do_POST instead, without the path.
        pass

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            self.wfile.write(data)
        except OSError:  # the client went away
            self.close_connection = True

    def _discard(self, length: int) -> int:
        """Read the body in chunks without keeping it. Returns the bytes read."""
        remaining = length
        try:
            while remaining:
                chunk = self.rfile.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:  # stalled (socket timeout) or reset
            pass
        return length - remaining

    def do_GET(self) -> None:
        if _route(self.path) == "/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if _route(self.path) != WEBHOOK_PATH:
            self._send(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self._send(400, {"error": "missing or invalid content-length"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large"})
            return
        if self._discard(length) != length:
            self.close_connection = True
            self._send(400, {"error": "incomplete body"})
            return

        LOG.info(
            "github delivery event=%s delivery=%s bytes=%d signature=%s",
            _clean(self.headers.get("X-GitHub-Event")),
            _clean(self.headers.get("X-GitHub-Delivery")),
            length,
            "yes" if self.headers.get("X-Hub-Signature-256") else "no",
        )
        self._send(200, {"status": "ok", "listener": "placeholder"})


def _terminate(signum: int, frame) -> None:
    raise SystemExit(0)


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    if port is None:
        # Not GITHUB_WEBHOOK_PORT: Kubernetes sets that name for Service github-webhook.
        port = int(os.environ.get("LISTEN_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), PlaceholderHandler)
    LOG.info("placeholder listening on %s:%s", host, server.server_address[1])
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # Python as PID 1 ignores SIGTERM unless a handler is installed.
    signal.signal(signal.SIGTERM, _terminate)
    server = serve()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LOG.info("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
