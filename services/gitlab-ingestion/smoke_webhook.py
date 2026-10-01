"""Local webhook smoke. No public URL and no GitLab call.

Starts the receiver, then POSTs signed Merge Request, Issue, Note and Work
Item hooks plus one retried duplicate and one forged delivery. Checks each
response and that exactly one {"type":"gitlab","body":...} line per distinct
delivery reaches the receiver stdout. Set GITLAB_WEBHOOK_SIGNING_TOKEN to any
throwaway whsec_ value (whsec_ plus base64).

Signing matches GitLab's signing token: webhook-id, webhook-timestamp, and
webhook-signature = v1,<base64 HMAC-SHA256 of "<id>.<timestamp>.<raw body>">,
keyed with the base64-decoded token after whsec_.
"""

from __future__ import annotations

import base64
import binascii
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

PROJECT = {"id": 42, "name": "widget", "path_with_namespace": "acme/widget", "web_url": "https://gitlab.com/acme/widget"}
USER = {"id": 7, "name": "Pat Example", "username": "pat"}

EVENTS = (
    (
        "Merge Request Hook",
        "msg_mr_open",
        {
            "object_kind": "merge_request",
            "event_type": "merge_request",
            "user": USER,
            "project": PROJECT,
            "object_attributes": {
                "id": 700,
                "iid": 7,
                "title": "Store intake as raw envelopes",
                "description": "Decision: the pipeline normalizes, intake does not.",
                "state": "opened",
                "action": "open",
                "source_branch": "feature/intake",
                "target_branch": "develop",
            },
            "labels": [{"title": "architecture"}],
        },
    ),
    (
        "Issue Hook",
        "msg_issue_open",
        {
            "object_kind": "issue",
            "event_type": "issue",
            "user": USER,
            "project": PROJECT,
            "object_attributes": {"id": 500, "iid": 12, "title": "Backfill misses notes", "state": "opened", "action": "open"},
        },
    ),
    (
        "Note Hook",
        "msg_note_mr",
        {
            "object_kind": "note",
            "event_type": "note",
            "user": {"id": 8, "name": "Sam Example", "username": "sam"},
            "project_id": 42,
            "project": PROJECT,
            "object_attributes": {
                "id": 901,
                "note": "Agreed. Keeping the body unchanged makes reprocessing possible.",
                "noteable_type": "MergeRequest",
                "discussion_id": "abc123",
            },
            "merge_request": {"iid": 7, "title": "Store intake as raw envelopes"},
        },
    ),
    (
        "Work Item Hook",
        "msg_work_item",
        {
            "object_kind": "work_item",
            "event_type": "work_item",
            "user": USER,
            "project": PROJECT,
            "iid": 13,
            "title": "Document reconciliation cadence",
            "object_attributes": {"id": 501, "iid": 13, "title": "Document reconciliation cadence", "action": "open"},
        },
    ),
)


def sign(key: bytes, message_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{message_id}.{timestamp}.".encode("utf-8") + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


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


def post(port: int, key: bytes, event: str, message_id: str, payload: dict, *, forge: bool = False) -> tuple[int, dict]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = sign(b"not-the-key" if forge else key, message_id, timestamp, raw)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/webhook/gitlab",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Event": event,
            "Idempotency-Key": message_id,
            "webhook-id": message_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            status = response.status
            reply = response.read()
    except urllib.error.HTTPError as exc:
        with exc:
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
    token = os.environ.get("GITLAB_WEBHOOK_SIGNING_TOKEN", "")
    if not token.startswith("whsec_"):
        print("Set GITLAB_WEBHOOK_SIGNING_TOKEN to a throwaway whsec_<base64> value.", file=sys.stderr)
        return 2
    try:
        key = base64.b64decode(token[len("whsec_") :], validate=True)
    except binascii.Error:
        print("GITLAB_WEBHOOK_SIGNING_TOKEN is not whsec_ plus base64.", file=sys.stderr)
        return 2

    port = free_port()
    env = os.environ.copy()
    env["GITLAB_WEBHOOK_SIGNING_TOKEN"] = token
    env.pop("GITLAB_WEBHOOK_SECRET_TOKEN", None)
    env["GITLAB_INGESTION_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(RECEIVER)],
        cwd=str(HERE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_until_ready(port, proc)
        for event, message_id, payload in EVENTS:
            status, reply = post(port, key, event, message_id, payload)
            if status != 200 or reply.get("status") != "accepted":
                print(f"{event}: HTTP {status} {reply}", file=sys.stderr)
                return 1
        event, message_id, payload = EVENTS[0]
        status, reply = post(port, key, event, message_id, payload)
        if status != 200 or reply.get("status") != "duplicate":
            print(f"retry: HTTP {status} {reply}", file=sys.stderr)
            return 1
        status, reply = post(port, key, "Issue Hook", "msg_forged", EVENTS[1][2], forge=True)
        if status != 401:
            print(f"forged delivery: HTTP {status} {reply}", file=sys.stderr)
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

    if proc.returncode not in (0, 1, -15, 143):
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

    for (event, _, payload), envelope in zip(EVENTS, envelopes):
        if set(envelope) != {"type", "body"} or envelope.get("type") != "gitlab":
            print(f"{event}: envelope is not {{\"type\":\"gitlab\",\"body\":...}}", file=sys.stderr)
            return 1
        if envelope["body"] != payload:
            print(f"{event}: body does not match the posted payload", file=sys.stderr)
            return 1
        print(json.dumps(envelope, separators=(",", ":")), flush=True)

    print(f"smoke ok: {len(envelopes)} envelopes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
