"""Local webhook smoke. No public URL and no Slack call.

Starts the receiver, answers a signed url_verification, then POSTs signed
message, edit, thread reply, reaction, and channel_created deliveries plus
one retried duplicate. Checks each response and that exactly one
{"type":"slack","body":...} line per distinct event reaches the receiver
stdout. Set SLACK_SIGNING_SECRET to any throwaway value.

Signing matches Slack: X-Slack-Request-Timestamp plus X-Slack-Signature,
v0=<hex HMAC-SHA256 of "v0:<timestamp>:<raw body>">.
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


def _callback(team: str, event_id: str, event: dict) -> dict:
    return {
        "token": "legacy-verification-token",
        "team_id": team,
        "api_app_id": "A0SMOKE",
        "type": "event_callback",
        "event_id": event_id,
        "event_time": 1758668948,
        "event": event,
    }


EVENTS = (
    (
        "message",
        _callback(
            "T0AAA",
            "Ev0001",
            {
                "type": "message",
                "channel": "C0DEC",
                "channel_type": "channel",
                "user": "U0ADA",
                "text": "Decision: the graph lives in Neo4j.",
                "ts": "1758668948.000100",
                "event_ts": "1758668948.000100",
            },
        ),
    ),
    (
        "message_changed",
        _callback(
            "T0AAA",
            "Ev0002",
            {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C0DEC",
                "hidden": True,
                "message": {"type": "message", "user": "U0ADA", "text": "Decision: the graph lives in Neo4j 5.", "ts": "1758668948.000100"},
                "previous_message": {"type": "message", "user": "U0ADA", "text": "Decision: the graph lives in Neo4j.", "ts": "1758668948.000100"},
                "ts": "1758668990.000200",
                "event_ts": "1758668990.000200",
            },
        ),
    ),
    (
        "thread reply",
        _callback(
            "T0BBB",
            "Ev0003",
            {
                "type": "message",
                "channel": "G0PRIV",
                "channel_type": "group",
                "user": "U0BEN",
                "text": "Agreed, Cypher is the reason.",
                "ts": "1758669000.000300",
                "thread_ts": "1758668948.000100",
                "event_ts": "1758669000.000300",
            },
        ),
    ),
    (
        "reaction_added",
        _callback(
            "T0AAA",
            "Ev0004",
            {
                "type": "reaction_added",
                "user": "U0BEN",
                "reaction": "white_check_mark",
                "item": {"type": "message", "channel": "C0DEC", "ts": "1758668948.000100"},
                "item_user": "U0ADA",
                "event_ts": "1758669010.000400",
            },
        ),
    ),
    (
        "channel_created",
        _callback(
            "T0AAA",
            "Ev0005",
            {
                "type": "channel_created",
                "channel": {"id": "C0NEW", "name": "architecture", "created": 1758669020, "creator": "U0ADA"},
                "event_ts": "1758669020.000500",
            },
        ),
    ),
)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = f"v0:{timestamp}:".encode("utf-8") + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


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


def post(port: int, secret: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/webhook/slack",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": sign(secret, timestamp, raw),
            **(headers or {}),
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
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        print("Set SLACK_SIGNING_SECRET to a throwaway local value.", file=sys.stderr)
        return 2

    port = free_port()
    env = os.environ.copy()
    env["SLACK_SIGNING_SECRET"] = secret
    env["SLACK_INGESTION_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(RECEIVER)],
        cwd=str(HERE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_until_ready(port, proc)
        status, reply = post(port, secret, {"token": "t", "challenge": "smoke-challenge", "type": "url_verification"})
        if status != 200 or reply != {"challenge": "smoke-challenge"}:
            print(f"url_verification: HTTP {status} {reply}", file=sys.stderr)
            return 1
        for name, payload in EVENTS:
            status, reply = post(port, secret, payload)
            if status != 200 or reply.get("status") != "accepted":
                print(f"{name}: HTTP {status} {reply}", file=sys.stderr)
                return 1
        status, reply = post(
            port,
            secret,
            EVENTS[0][1],
            {"X-Slack-Retry-Num": "1", "X-Slack-Retry-Reason": "http_timeout"},
        )
        if status != 200 or reply.get("status") != "duplicate":
            print(f"retry: HTTP {status} {reply}", file=sys.stderr)
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

    for (name, payload), envelope in zip(EVENTS, envelopes):
        if set(envelope) != {"type", "body"} or envelope.get("type") != "slack":
            print(f"{name}: envelope is not {{\"type\":\"slack\",\"body\":...}}", file=sys.stderr)
            return 1
        if envelope["body"] != payload:
            print(f"{name}: body does not match the posted payload", file=sys.stderr)
            return 1
        print(json.dumps(envelope, separators=(",", ":")), flush=True)

    print(f"smoke ok: {len(envelopes)} envelopes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
