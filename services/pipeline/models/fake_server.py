"""A local stand-in for an OpenAI-compatible API. Used by tests so CI never spends money."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        server = self.server
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        auth = self.headers.get("Authorization", "")
        server.requests.append((self.path, auth, payload))
        if auth != f"Bearer {server.key}":
            return self._send(401, {"error": {"message": f"bad key {auth.removeprefix('Bearer ')}"}})
        if server.fail:
            return self._send(server.fail.pop(0), {"error": {"message": "try later"}})
        if self.path.endswith("/chat/completions"):
            return self._send(200, {"choices": [{"message": {"content": "pong"}}]})
        if self.path.endswith("/embeddings"):
            rows = [{"index": i, "embedding": [len(text) / 10 + j for j in range(server.dims)]}
                    for i, text in enumerate(payload["input"])]
            return self._send(200, {"data": list(reversed(rows))})  # out of order on purpose
        self._send(404, {"error": {"message": "no such path"}})


class FakeServer:
    def __init__(self, dims: int = 4, fail: list[int] | None = None, key: str = "sk-test") -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.dims, self._server.fail = dims, list(fail or [])
        self._server.key, self._server.requests = key, []

    @property
    def requests(self):
        return self._server.requests

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
