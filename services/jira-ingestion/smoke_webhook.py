"""Local webhook smoke. No public URL and no Jira call.

Starts the receiver, POSTs HMAC-signed issue deliveries, and checks each
response is 202 with one {"type":"jira","body":...} line on the receiver
stdout. Set JIRA_WEBHOOK_SECRET to any throwaway value.

The signature header is X-Hub-Signature (sha256=<hex>), which is what Jira
admin webhooks send when a secret is set. A query-string secret is not used.
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
        "jira:issue_created",
        {
            "timestamp": 1758668948000,
            "webhookEvent": "jira:issue_created",
            "cloudId": "site-a",
            "issue": {
                "id": "10231",
                "key": "EMBER-31",
                "fields": {
                    "summary": "Start Jira ingestion",
                    "description": "Spike the connector and keep the description.",
                    "issuetype": {"name": "Story"},
                    "project": {"id": "10000", "key": "EMBER"},
                    "status": {"name": "In Progress"},
                },
            },
        },
    ),
    (
        "jira:issue_updated",
        {
            "timestamp": 1758669000000,
            "webhookEvent": "jira:issue_updated",
            "cloudId": "site-b",
            "issue": {
                "id": "20001",
                "key": "OTHER-4",
                "fields": {
                    "summary": "Notes from another site",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "ADF description stays in the body."}],
                            }
                        ],
                    },
                    "project": {"id": "20000", "key": "OTHER"},
                    "status": {"name": "To Do"},
                },
            },
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
        f"http://127.0.0.1:{port}/webhook/jira",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Atlassian-Webhook-Identifier": f"smoke-{event}",
            "X-Hub-Signature": sign(secret, raw),
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
    secret = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    if not secret:
        print("Set JIRA_WEBHOOK_SECRET to a throwaway local value.", file=sys.stderr)
        return 2

    port = free_port()
    env = os.environ.copy()
    env["JIRA_WEBHOOK_SECRET"] = secret
    env["JIRA_INGESTION_PORT"] = str(port)
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

    if proc.returncode not in (0, -15, 143):
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
        if set(envelope) != {"type", "body"} or envelope.get("type") != "jira":
            print(f"{event}: envelope is not {{\"type\":\"jira\",\"body\":...}}", file=sys.stderr)
            return 1
        if envelope["body"] != payload:
            print(f"{event}: body does not match the posted payload", file=sys.stderr)
            return 1
        print(json.dumps(envelope, separators=(",", ":")), flush=True)

    print(f"smoke ok: {len(envelopes)} envelopes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
