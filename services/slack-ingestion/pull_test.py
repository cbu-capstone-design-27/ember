"""Local Slack pull for the channels the Ember bot is in.

Reads a workspace with a bot token (xoxb-) and writes one EMBER-39
envelope per discrete JSON object: the workspace (team.info), each member
(users.list), each channel the bot is a member of (conversations.list),
each top-level message (conversations.history), and each thread reply
(conversations.replies). Channels the bot has not been invited to are not
read. Direct messages are out of scope. File bytes are not downloaded;
file metadata stays inside the message that shared it.

Machine intake is JSONL, one object per line:
  {"type":"slack","body":<raw Slack object>}
The one change to a raw object: a history or reply message has no channel
id of its own, so "channel" (the field name Slack uses on message events)
is set to the channel it was read from when the message does not carry one.

Stdout is that JSONL. --output FILE writes the same JSONL and, by default,
a glance summary at FILE.readable.md. --readable PATH chooses the summary
file, --readable - prints it on stdout (needs --output), --no-readable
skips it. Progress goes to stderr.

Slack limits each Web API method separately. Each method has its own gate:
a 429 holds every worker calling that method for Retry-After seconds and
leaves the other methods running. A 5xx backs off 1s, 2s, 4s, ...
--since limits conversations.history, so a thread whose parent is older
than --since is not read even if it has newer replies.
Channels are read DEFAULT_CONCURRENCY at a time (--concurrency or
SLACK_PULL_CONCURRENCY). Pages inside one channel stay in order.

The webhook receiver does not use this script. SLACK_TEST_CHANNEL (or
--channel) narrows one run to one channel id. Nothing here filters live
deliveries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from envelope import wrap_slack

API_BASE = "https://slack.com/api"
USER_AGENT = "ember-slack-ingestion"
PAGE_LIMIT = 200
# Safety stop for a cursor that never ends: 500 pages is 100,000 items
# per channel (or member list). A bigger channel needs --since.
MAX_PAGES = 500
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16
MAX_RETRY_WAIT = 120.0
CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{2,}$")
CONVERSATION_TYPES = "public_channel,private_channel"

# Slack "ok": false errors that mean the token itself is unusable.
AUTH_ERRORS = {"not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired"}
# Errors that skip one channel and let the rest of the pull continue.
CHANNEL_SKIP_ERRORS = {"not_in_channel", "channel_not_found"}


class PullError(Exception):
    pass


class SlackApiError(PullError):
    """Slack answered HTTP 200 with {"ok": false, "error": ...}."""

    def __init__(self, api_method: str, error: str, needed: str | None = None) -> None:
        self.api_method = api_method
        self.error = error
        self.needed = needed
        if error in AUTH_ERRORS:
            message = f"{api_method}: SLACK_BOT_TOKEN was rejected ({error}). Reinstall the app or copy a fresh token."
        elif error == "missing_scope":
            message = (
                f"{api_method}: the Slack app is missing scope {needed or '?'}. "
                "Add it under OAuth & Permissions (or from slack-app-manifest.json) and reinstall."
            )
        else:
            message = f"{api_method} failed: {error}"
        super().__init__(message)


class RateLimitGate:
    """Holds every worker until a shared throttle window has passed."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._until = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                delay = self._until - self._clock()
            if delay <= 0:
                return
            self._sleep(delay)

    def extend(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            target = self._clock() + seconds
            if target > self._until:
                self._until = target


class MethodGates:
    """One RateLimitGate per Slack API method, created on first use."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._gates: dict[str, RateLimitGate] = {}

    def __getitem__(self, api_method: str) -> RateLimitGate:
        with self._lock:
            gate = self._gates.get(api_method)
            if gate is None:
                gate = RateLimitGate(self._clock, self._sleep)
                self._gates[api_method] = gate
            return gate


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def bot_token() -> str:
    """The one place a token is read. Today an env var; later the
    Application Database's integration settings for this workspace."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise PullError("Set SLACK_BOT_TOKEN to the app's Bot User OAuth Token (xoxb-...).")
    if not token.startswith(("xoxb-", "xoxe.xoxb-")):
        raise PullError(
            "SLACK_BOT_TOKEN must be a bot token (xoxb-...). User (xoxp-) and "
            "app-level (xapp-) tokens are not used by this worker."
        )
    return token


def resolve_concurrency(cli_value: int | None) -> int:
    """CLI wins, then SLACK_PULL_CONCURRENCY, then DEFAULT_CONCURRENCY."""
    if cli_value is None:
        raw = os.environ.get("SLACK_PULL_CONCURRENCY", "").strip()
        if not raw:
            return DEFAULT_CONCURRENCY
        try:
            cli_value = int(raw)
        except ValueError as exc:
            raise PullError("SLACK_PULL_CONCURRENCY must be an integer") from exc
    if cli_value < 1 or cli_value > MAX_CONCURRENCY:
        raise PullError(f"concurrency must be from 1 to {MAX_CONCURRENCY}")
    return cli_value


def resolve_channel(cli_value: str | None) -> str | None:
    """CLI wins, then SLACK_TEST_CHANNEL. None means every channel the bot is in."""
    raw = cli_value if cli_value is not None else os.environ.get("SLACK_TEST_CHANNEL", "")
    channel = raw.strip()
    if not channel:
        return None
    if not CHANNEL_ID.fullmatch(channel):
        raise PullError(
            "SLACK_TEST_CHANNEL must be a channel id such as C0123ABCD "
            "(channel details -> bottom of the About tab), not a #name."
        )
    return channel


def parse_since(value: str | None) -> str | None:
    """YYYY-MM-DD (UTC midnight) or a Unix timestamp, as Slack's oldest param."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise PullError("--since must be YYYY-MM-DD or a Unix timestamp") from exc
        seconds = day.timestamp()
    if seconds < 0:
        raise PullError("--since must not be negative")
    return f"{seconds:.6f}"


def retry_after_seconds(response: Response, attempt: int = 0) -> float | None:
    """Seconds to wait on a Slack throttle, or None when the response is final.

    Retry-After wins. A 429 without it waits 1s, a 5xx waits 2**attempt.
    """
    if response.status < 400:
        return None
    headers = {key.lower(): value for key, value in response.headers.items()}
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    if response.status == 429:
        return 1.0
    if response.status in (500, 502, 503, 504):
        return float(2 ** max(0, attempt))
    return None


def urllib_exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())


def map_ordered(fn, items: list, workers: int) -> list:
    """Run fn over items concurrently and return results in input order."""
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


class SlackClient:
    """GET a Slack Web API method with a bearer token. The token is sent in
    the Authorization header only, so it never appears in a URL or a log."""

    def __init__(self, token: str, exchange, gates: MethodGates, *, attempts: int = 5) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        self._exchange = exchange
        self._gates = gates
        self._attempts = attempts

    def call(self, api_method: str, params: dict | None = None) -> dict:
        query = urlencode(params or {})
        url = f"{API_BASE}/{api_method}" + (f"?{query}" if query else "")
        gate = self._gates[api_method]
        response = None
        for attempt in range(self._attempts):
            gate.wait()
            response = self._exchange("GET", url, self._headers, None)
            wait = retry_after_seconds(response, attempt)
            if wait is None:
                break
            if attempt == self._attempts - 1:
                raise PullError(f"{api_method} still throttled after {self._attempts} attempts")
            wait = min(wait, MAX_RETRY_WAIT)
            print(f"Slack asked to wait {wait:.0f}s ({response.status}) {api_method}", file=sys.stderr)
            gate.extend(wait)
        if response is None:
            raise PullError(f"{api_method} was not called")
        if response.status >= 400:
            detail = response.body.decode("utf-8", "replace")[:300]
            raise PullError(f"{api_method} failed (HTTP {response.status}): {detail}")
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PullError(f"{api_method} did not return JSON") from exc
        if not isinstance(payload, dict):
            raise PullError(f"{api_method} did not return a JSON object")
        if payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload.get("error"), str) else "unknown_error"
            needed = payload.get("needed") if isinstance(payload.get("needed"), str) else None
            raise SlackApiError(api_method, error, needed)
        return payload

    def paged(self, api_method: str, params: dict, field: str) -> Iterator[dict]:
        """Follow response_metadata.next_cursor. Items are yielded unchanged."""
        cursor = ""
        for _ in range(MAX_PAGES):
            query = dict(params, limit=PAGE_LIMIT)
            if cursor:
                query["cursor"] = cursor
            page = self.call(api_method, query)
            items = page.get(field)
            if not isinstance(items, list):
                raise PullError(f"{api_method} had no {field} list")
            for item in items:
                if not isinstance(item, dict):
                    raise PullError(f"{api_method} returned a non-object in {field}")
                yield item
            meta = page.get("response_metadata")
            following = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not isinstance(following, str) or not following:
                return
            if following == cursor:
                raise PullError(f"{api_method} pagination repeated its cursor")
            cursor = following
        raise PullError(
            f"stopped after {MAX_PAGES} pages of {api_method}; use --since to read a shorter window"
        )


def _ts_key(message: dict) -> float:
    try:
        return float(message.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _with_channel(message: dict, channel_id: str) -> dict:
    if "channel" not in message:
        message["channel"] = channel_id
    return message


def select_channels(
    client: SlackClient,
    only: str | None,
    *,
    include_archived: bool = False,
) -> list[dict]:
    """Channel objects the bot is a member of, in conversations.list order."""
    params = {"types": CONVERSATION_TYPES, "exclude_archived": "false" if include_archived else "true"}
    listed = list(client.paged("conversations.list", params, "channels"))
    if only is not None:
        for channel in listed:
            if channel.get("id") == only:
                if channel.get("is_member") is not True:
                    raise PullError(
                        f"the bot is not a member of {only} (#{channel.get('name', '?')}). "
                        "Run /invite @Ember in that channel first."
                    )
                return [channel]
        raise PullError(
            f"channel {only} was not found. It may be archived (--include-archived), "
            "a DM, or in another workspace."
        )
    members = [channel for channel in listed if channel.get("is_member") is True]
    if not members:
        print(
            "the bot is not a member of any channel yet; run /invite @Ember in a channel "
            "to include its history",
            file=sys.stderr,
        )
    return members


def channel_messages(client: SlackClient, channel: dict, oldest: str | None) -> list[dict]:
    """Top-level messages and their thread replies, oldest first.

    Each reply follows its parent. The parent copy that conversations.replies
    returns first is not repeated. A reply also broadcast to the channel is
    emitted once, where it first appears.
    """
    channel_id = channel["id"]
    history_params = {"channel": channel_id}
    if oldest is not None:
        history_params["oldest"] = oldest
    try:
        top = list(client.paged("conversations.history", history_params, "messages"))
    except SlackApiError as exc:
        if exc.error in CHANNEL_SKIP_ERRORS:
            print(f"skipped {channel_id}: {exc.error}", file=sys.stderr)
            return []
        raise
    top.sort(key=_ts_key)

    seen: set[str] = set()
    ordered: list[dict] = []
    for message in top:
        ts = message.get("ts")
        if isinstance(ts, str):
            if ts in seen:
                continue
            seen.add(ts)
        ordered.append(_with_channel(message, channel_id))
        reply_count = message.get("reply_count")
        thread_ts = message.get("thread_ts")
        if not isinstance(reply_count, int) or reply_count <= 0 or thread_ts != ts:
            continue
        params = {"channel": channel_id, "ts": ts}
        try:
            replies = list(client.paged("conversations.replies", params, "messages"))
        except SlackApiError as exc:
            if exc.error in CHANNEL_SKIP_ERRORS or exc.error == "thread_not_found":
                print(f"skipped thread {channel_id}/{ts}: {exc.error}", file=sys.stderr)
                continue
            raise
        replies.sort(key=_ts_key)
        for reply in replies:
            reply_ts = reply.get("ts")
            if reply_ts == ts or (isinstance(reply_ts, str) and reply_ts in seen):
                continue
            if isinstance(reply_ts, str):
                seen.add(reply_ts)
            ordered.append(_with_channel(reply, channel_id))
    return ordered


def collect(
    client: SlackClient,
    *,
    only_channel: str | None = None,
    oldest: str | None = None,
    include_archived: bool = False,
    workers: int = DEFAULT_CONCURRENCY,
) -> list[dict]:
    """Raw objects in output order: team, users, then each channel followed
    by its messages and replies."""
    team_page = client.call("team.info")
    team = team_page.get("team")
    if not isinstance(team, dict):
        raise PullError("team.info had no team object")
    users = list(client.paged("users.list", {}, "members"))
    channels = select_channels(client, only_channel, include_archived=include_archived)
    per_channel = map_ordered(lambda channel: channel_messages(client, channel, oldest), channels, workers)

    bodies: list[dict] = [team]
    bodies.extend(users)
    for channel, messages in zip(channels, per_channel):
        bodies.append(channel)
        bodies.extend(messages)
    return bodies


def pull(
    token: str,
    exchange,
    *,
    only_channel: str | None = None,
    oldest: str | None = None,
    include_archived: bool = False,
    workers: int = DEFAULT_CONCURRENCY,
    gates: MethodGates | None = None,
) -> Iterator[dict]:
    client = SlackClient(token, exchange, gates or MethodGates())
    bodies = collect(
        client,
        only_channel=only_channel,
        oldest=oldest,
        include_archived=include_archived,
        workers=workers,
    )
    for body in bodies:
        yield wrap_slack(body)


def emit_jsonl(envelopes, stream) -> int:
    count = 0
    for envelope in envelopes:
        stream.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        count += 1
    stream.flush()
    return count


def readable_sidecar(output: Path) -> Path:
    """Glance file written next to an --output JSONL path."""
    return Path(str(output) + ".readable.md")


GLANCE_LIMIT = 120
SUMMARY_SECTIONS = (
    ("team", "Workspace"),
    ("channel", "Channels"),
    ("user", "Members"),
    ("message", "Messages"),
    ("other", "Other"),
)


def classify(body: dict) -> str:
    """Sidecar label for a raw object. The JSONL body is not changed."""
    if body.get("type") == "message" and "ts" in body:
        return "message"
    if body.get("is_channel") is True or body.get("is_group") is True or "is_member" in body:
        return "channel"
    if isinstance(body.get("profile"), dict) and "is_bot" in body:
        return "user"
    if isinstance(body.get("domain"), str) and isinstance(body.get("id"), str):
        return "team"
    return "other"


def excerpt(value, limit: int = GLANCE_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    flat = " ".join(value.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _user_name(user: dict) -> str:
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    for value in (profile.get("display_name"), profile.get("real_name"), user.get("real_name"), user.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(user.get("id") or "?")


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def render_summary(envelopes: list[dict]) -> str:
    """Glance lines for a human. Does not replace or rewrite the JSONL intake."""
    bodies = [env.get("body") for env in envelopes if isinstance(env.get("body"), dict)]
    names = {body["id"]: _user_name(body) for body in bodies if classify(body) == "user" and "id" in body}
    channel_names = {
        body["id"]: body.get("name") or body["id"] for body in bodies if classify(body) == "channel" and "id" in body
    }
    title = "Slack"
    groups: dict[str, list[str]] = {kind: [] for kind, _heading in SUMMARY_SECTIONS}
    for body in bodies:
        kind = classify(body)
        if kind == "team":
            title = f"Slack: {body.get('name') or body.get('id')}"
            groups[kind].append(f"{body.get('name', '?')} ({body.get('id', '?')}, {body.get('domain', '?')})")
        elif kind == "channel":
            privacy = "private" if body.get("is_private") else "public"
            groups[kind].append(f"#{body.get('name', '?')} ({body.get('id', '?')}, {privacy})")
        elif kind == "user":
            flags = " bot" if body.get("is_bot") else ""
            flags += " deleted" if body.get("deleted") else ""
            groups[kind].append(f"{_user_name(body)} ({body.get('id', '?')}){flags}")
        elif kind == "message":
            channel = channel_names.get(body.get("channel"), body.get("channel") or "?")
            who = names.get(body.get("user"), body.get("username") or body.get("user") or body.get("bot_id") or "?")
            is_reply = body.get("thread_ts") not in (None, body.get("ts"))
            lead = "  ↳ " if is_reply else ""
            subtype = f" [{body['subtype']}]" if isinstance(body.get("subtype"), str) else ""
            replies = body.get("reply_count")
            tail = f" ({replies} replies)" if isinstance(replies, int) and replies and not is_reply else ""
            groups[kind].append(
                f"{lead}#{channel} {_when(body.get('ts'))} {who}{subtype} — {excerpt(body.get('text')) or '(no text)'}{tail}"
            )
        else:
            groups["other"].append(str(body.get("id") or "?"))
    parts = [f"# {title}", ""]
    for kind, heading in SUMMARY_SECTIONS:
        labels = groups[kind]
        parts.append(f"## {heading} ({len(labels)})")
        parts.append("")
        parts.append("\n".join(f"- {label}" for label in labels) if labels else "- (none)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pull the Slack channels the Ember bot is in into EMBER-39 JSONL. "
            "With --output, also write a glance summary at <output>.readable.md."
        )
    )
    parser.add_argument(
        "--output",
        help="Write one-line EMBER-39 JSONL here instead of stdout. "
        "Also writes <output>.readable.md unless --no-readable.",
    )
    parser.add_argument(
        "--readable",
        metavar="PATH",
        help="Write the glance summary to PATH. Use - to print it on stdout "
        "(requires --output, so JSONL stays in the file).",
    )
    parser.add_argument("--no-readable", action="store_true", help="Skip the glance summary. JSONL only.")
    parser.add_argument(
        "--channel",
        help="One channel id (C.../G...). Defaults to SLACK_TEST_CHANNEL, then every channel the bot is in.",
    )
    parser.add_argument("--since", help="Only messages at or after this time: YYYY-MM-DD (UTC) or a Unix timestamp.")
    parser.add_argument("--include-archived", action="store_true", help="Also read archived channels the bot is in.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Channels read in parallel (default {DEFAULT_CONCURRENCY}, max {MAX_CONCURRENCY}, "
        "or SLACK_PULL_CONCURRENCY). Pages inside a channel stay serial.",
    )
    args = parser.parse_args(argv)
    if args.no_readable and args.readable:
        print("Use only one of --readable or --no-readable.", file=sys.stderr)
        return 2
    if args.readable == "-" and not args.output:
        print("--readable - needs --output so JSONL is not mixed into stdout.", file=sys.stderr)
        return 2

    try:
        token = bot_token()
        channel = resolve_channel(args.channel)
        oldest = parse_since(args.since)
        workers = resolve_concurrency(args.concurrency)
        envelopes = list(
            pull(
                token,
                urllib_exchange,
                only_channel=channel,
                oldest=oldest,
                include_archived=args.include_archived,
                workers=workers,
            )
        )
        if args.output:
            output = Path(args.output)
            with output.open("w", encoding="utf-8") as handle:
                count = emit_jsonl(envelopes, handle)
            print(f"wrote {count} envelopes to {output}", file=sys.stderr)
        else:
            count = emit_jsonl(envelopes, sys.stdout)
            print(f"wrote {count} envelopes", file=sys.stderr)
        if not args.no_readable:
            summary = render_summary(envelopes)
            if args.readable == "-":
                sys.stdout.write(summary)
            else:
                summary_path = Path(args.readable) if args.readable else None
                if summary_path is None and args.output:
                    summary_path = readable_sidecar(Path(args.output))
                if summary_path is not None:
                    summary_path.write_text(summary, encoding="utf-8")
                    print(f"wrote glance summary to {summary_path}", file=sys.stderr)
    except PullError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
