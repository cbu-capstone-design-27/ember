"""Local webhook smoke. No public URL and no GitHub call.

Starts the receiver, POSTs HMAC-signed deliveries for the event names in
EVENTS, and checks each response is 202 with one {"type":"github","body":...}
line on the receiver stdout. The event name is not a filter. Set
GITHUB_WEBHOOK_SECRET to any throwaway value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECEIVER = HERE / "receiver.py"

EVENTS = (
    (
        "issues",
        {
            "action": "opened",
            "issue": {
                "number": 12,
                "title": "Login redirect drops the query string",
                "state": "open",
                "body": "Repro: sign in with ?next=/settings and land on /.",
            },
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
    (
        "pull_request",
        {
            "action": "opened",
            "pull_request": {
                "number": 3,
                "title": "Add parser",
                "state": "open",
                "body": "What this changes.",
            },
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
    (
        "issue_comment",
        {
            "action": "created",
            "issue": {"number": 12},
            "comment": {"id": 9, "body": "thanks", "user": {"login": "octocat"}},
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
    (
        "pull_request_review",
        {
            "action": "submitted",
            "review": {"id": 4, "body": "LGTM", "state": "approved"},
            "pull_request": {"number": 3},
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
    (
        "pull_request_review_comment",
        {
            "action": "created",
            "comment": {"id": 8, "body": "nit"},
            "pull_request": {"number": 3},
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
    (
        "release",
        {
            "action": "published",
            "release": {"tag_name": "v1.0.0", "name": "v1", "body": "notes"},
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
    (
        "create",
        {
            "ref": "feature",
            "ref_type": "branch",
            "repository": {"full_name": "acme/widget"},
            "installation": {"id": 1},
        },
    ),
)


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_ready(port: int, proc: subprocess.Popen) -> None:
    deadline = time.time() + 5
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("receiver exited before /health")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.05)
    raise RuntimeError("receiver did not become ready")


def post_event(port: int, secret: str, event: str, payload: dict) -> tuple[int, dict]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/webhook/github",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": f"smoke-{event}",
            "X-Hub-Signature-256": sign(secret, raw),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            status = response.status
            reply = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        reply = exc.read()
    return status, json.loads(reply.decode("utf-8"))


def envelopes_from_stdout(raw: bytes) -> list[dict]:
    found = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        found.append(json.loads(line))
    return found


def main() -> int:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("Set GITHUB_WEBHOOK_SECRET to a throwaway local value.", file=sys.stderr)
        return 2

    port = free_port()
    env = os.environ.copy()
    env["GITHUB_WEBHOOK_SECRET"] = secret
    env["GITHUB_INGESTION_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(RECEIVER)],
        cwd=str(HERE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_until_ready(port, proc)
        for event, payload in EVENTS:
            status, reply = post_event(port, secret, event, payload)
            if status != 202 or reply.get("status") != "accepted":
                print(f"{event}: HTTP {status} {reply}", file=sys.stderr)
                return 1
    except (RuntimeError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

    if proc.returncode not in (0, -15, 143, None) and proc.returncode != 0:
        # terminate yields SIGTERM (-15 / 143). Any other code is a crash.
        if proc.returncode not in (-15, 143):
            sys.stderr.write(stderr.decode("utf-8", "replace"))
            print(f"receiver exit {proc.returncode}", file=sys.stderr)
            return 1

    try:
        envelopes = envelopes_from_stdout(stdout)
    except json.JSONDecodeError as exc:
        print(f"receiver stdout was not JSONL: {exc}", file=sys.stderr)
        return 1

    if len(envelopes) != len(EVENTS):
        sys.stderr.write(stderr.decode("utf-8", "replace"))
        print(f"expected {len(EVENTS)} envelopes, got {len(envelopes)}", file=sys.stderr)
        return 1

    for (event, payload), envelope in zip(EVENTS, envelopes):
        if set(envelope) != {"type", "body"} or envelope.get("type") != "github":
            print(f"{event}: envelope is not {{\"type\":\"github\",\"body\":...}}", file=sys.stderr)
            return 1
        if envelope["body"] != payload:
            print(f"{event}: body does not match the posted payload", file=sys.stderr)
            return 1
        print(json.dumps(envelope, separators=(",", ":")), flush=True)

    print(f"smoke ok: {len(envelopes)} envelopes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
