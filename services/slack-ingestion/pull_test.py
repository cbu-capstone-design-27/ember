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

Rate limits (EMBER-52). Slack limits each Web API method per workspace per
app, by tier. Each method has its own gate:
- Pacing: calls are spaced at the method's tier rate (Tier 2: 20/min,
  Tier 3: 50/min), so workers do not burst into throttling.
  --rate-mode non_marketplace (or SLACK_RATE_MODE) drops history and
  replies to 1/min and 15 per page, Slack's limit for commercially
  distributed apps outside the Marketplace.
- Throttling is not failure: a 429 or a "ratelimited" error waits the full
  Retry-After and retries, with no attempt limit, until one call has waited
  THROTTLE_BUDGET seconds in total.
- Transient errors (5xx, timeouts, resets, DNS) back off 1s, 2s, 4s, ...
  with jitter, up to TRANSIENT_ATTEMPTS tries.
- Real errors (invalid token, missing scope) stop at once.

Durable runs. With --output, every page is appended to a part file under
<output>.parts/ and a checkpoint (<output>.checkpoint.json) is saved after
it. --resume continues from the checkpoint, so a crash costs at most one
page. The final JSONL is assembled at the end, in the same order as a
single run, with duplicates from a resumed page removed. Without --output
the pull runs in memory and cannot resume.

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
import http.client
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from envelope import wrap_slack

API_BASE = "https://slack.com/api"
USER_AGENT = "ember-slack-ingestion"
PAGE_LIMIT = 200
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16
CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{2,}$")
CONVERSATION_TYPES = "public_channel,private_channel"

# Status used for a request that never got an HTTP answer (timeout, reset, DNS).
NETWORK_ERROR = 0
TRANSIENT_STATUSES = {NETWORK_ERROR, 500, 502, 503, 504}

# Throttling (429 or "ratelimited") is waited out, never counted as a failure.
MAX_THROTTLE_WAIT = 900.0  # cap on one Retry-After (15 min)
THROTTLE_BUDGET = 3600.0  # total throttle wait one call may accumulate
RATELIMITED_DEFAULT_WAIT = 30.0  # "ratelimited" body without a Retry-After header

# Transient errors get a bounded, jittered exponential backoff.
TRANSIENT_ATTEMPTS = 8
TRANSIENT_BASE_WAIT = 1.0
TRANSIENT_MAX_WAIT = 60.0

# Pacing per method (calls per minute). Slack tiers: docs.slack.dev, 2026-10-01.
RATE_MODES = ("internal", "non_marketplace")
DEFAULT_RATE_MODE = "internal"
TIER_PER_MINUTE = {
    "team.info": 50.0,  # Tier 3
    "users.list": 20.0,  # Tier 2
    "conversations.list": 20.0,  # Tier 2
    "conversations.history": 50.0,  # Tier 3
    "conversations.replies": 50.0,  # Tier 3
}
# Commercially distributed apps not approved for the Marketplace.
NON_MARKETPLACE_PER_MINUTE = {"conversations.history": 1.0, "conversations.replies": 1.0}
NON_MARKETPLACE_PAGE_LIMIT = 15

CHECKPOINT_VERSION = 1

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
    """Paces calls to one method and holds every worker during a throttle.

    ``interval`` is the minimum spacing between calls (0 disables pacing).
    ``extend`` pushes the whole gate back, for a Retry-After or a backoff.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 0.0,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._until = 0.0
        self._next = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                delay = max(self._until, self._next) - now
                if delay <= 0:
                    self._next = now + self._interval
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
    """One RateLimitGate per Slack API method, created on first use.

    ``rates`` maps a method to calls per minute. Methods not in it, or
    ``rates=None``, are not paced (throttles still hold them).
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rates: dict[str, float] | None = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._rates = dict(rates or {})
        self._lock = threading.Lock()
        self._gates: dict[str, RateLimitGate] = {}

    def __getitem__(self, api_method: str) -> RateLimitGate:
        with self._lock:
            gate = self._gates.get(api_method)
            if gate is None:
                rate = self._rates.get(api_method)
                interval = 60.0 / rate if rate else 0.0
                gate = RateLimitGate(self._clock, self._sleep, interval)
                self._gates[api_method] = gate
            return gate


def rate_table(mode: str) -> dict[str, float]:
    """Calls per minute for each method in a rate mode."""
    if mode not in RATE_MODES:
        raise PullError(f"rate mode must be one of {', '.join(RATE_MODES)}")
    rates = dict(TIER_PER_MINUTE)
    if mode == "non_marketplace":
        rates.update(NON_MARKETPLACE_PER_MINUTE)
    return rates


def page_limits(mode: str) -> dict[str, int]:
    """Page size per method. Methods not listed use PAGE_LIMIT."""
    if mode == "non_marketplace":
        return {method: NON_MARKETPLACE_PAGE_LIMIT for method in NON_MARKETPLACE_PER_MINUTE}
    return {}


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


def resolve_rate_mode(cli_value: str | None) -> str:
    """CLI wins, then SLACK_RATE_MODE, then DEFAULT_RATE_MODE."""
    raw = cli_value if cli_value is not None else os.environ.get("SLACK_RATE_MODE", "")
    mode = raw.strip().lower() or DEFAULT_RATE_MODE
    if mode not in RATE_MODES:
        raise PullError(f"SLACK_RATE_MODE must be one of {', '.join(RATE_MODES)}")
    return mode


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


def _header_seconds(response: Response, name: str) -> float | None:
    headers = {key.lower(): value for key, value in response.headers.items()}
    raw = headers.get(name)
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def retry_after_seconds(response: Response, attempt: int = 0) -> float | None:
    """Seconds to wait before retrying, or None when the response is final.

    Retry-After wins. A 429 without it waits 1s; a transient error (5xx or
    no HTTP answer) waits 2**attempt.
    """
    if NETWORK_ERROR < response.status < 400:
        return None
    retry_after = _header_seconds(response, "retry-after")
    if retry_after is not None:
        return retry_after
    if response.status == 429:
        return 1.0
    if response.status in TRANSIENT_STATUSES:
        return float(2 ** max(0, attempt))
    return None


def urllib_exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    """One HTTP request. A request that never gets an HTTP answer comes back
    as status NETWORK_ERROR so the caller can retry it."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        return Response(NETWORK_ERROR, {}, f"{type(exc).__name__}: {exc}".encode("utf-8", "replace"))


def map_ordered(fn, items: list, workers: int) -> list:
    """Run fn over items concurrently and return results in input order."""
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class SlackClient:
    """GET a Slack Web API method with a bearer token. The token is sent in
    the Authorization header only, so it never appears in a URL or a log."""

    def __init__(
        self,
        token: str,
        exchange,
        gates: MethodGates,
        *,
        page_limits: dict[str, int] | None = None,
        throttle_budget: float = THROTTLE_BUDGET,
        transient_attempts: int = TRANSIENT_ATTEMPTS,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        self._exchange = exchange
        self._gates = gates
        self._page_limits = dict(page_limits or {})
        self._throttle_budget = throttle_budget
        self._transient_attempts = transient_attempts
        self._jitter = jitter or (lambda: random.uniform(0.5, 1.0))

    def page_limit(self, api_method: str) -> int:
        return self._page_limits.get(api_method, PAGE_LIMIT)

    def _throttle_wait(self, response: Response, default: float) -> float:
        wait = _header_seconds(response, "retry-after")
        if wait is None:
            wait = default
        return min(max(wait, 1.0), MAX_THROTTLE_WAIT)

    def call(self, api_method: str, params: dict | None = None) -> dict:
        query = urlencode(params or {})
        url = f"{API_BASE}/{api_method}" + (f"?{query}" if query else "")
        gate = self._gates[api_method]
        throttled = 0.0
        failures = 0
        while True:
            gate.wait()
            response = self._exchange("GET", url, self._headers, None)

            payload = None
            if response.status == 200:
                try:
                    payload = response.json()
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PullError(f"{api_method} did not return JSON") from exc
                if not isinstance(payload, dict):
                    raise PullError(f"{api_method} did not return a JSON object")

            ratelimited_body = isinstance(payload, dict) and payload.get("error") == "ratelimited"
            if response.status == 429 or ratelimited_body:
                wait = self._throttle_wait(response, 1.0 if response.status == 429 else RATELIMITED_DEFAULT_WAIT)
                if throttled + wait > self._throttle_budget:
                    raise PullError(
                        f"{api_method} still throttled after waiting {throttled:.0f}s; "
                        "Slack may be limiting this app harder than its tier (see SLACK_RATE_MODE)"
                    )
                throttled += wait
                _log(f"Slack asked to wait {wait:.0f}s (rate limited) {api_method}")
                gate.extend(wait)
                continue

            if response.status in TRANSIENT_STATUSES:
                failures += 1
                if failures >= self._transient_attempts:
                    detail = response.body.decode("utf-8", "replace")[:200]
                    what = "network error" if response.status == NETWORK_ERROR else f"HTTP {response.status}"
                    raise PullError(f"{api_method} failed after {failures} attempts ({what}): {detail}")
                wait = _header_seconds(response, "retry-after")
                if wait is None:
                    backoff = min(TRANSIENT_BASE_WAIT * 2 ** (failures - 1), TRANSIENT_MAX_WAIT)
                    wait = backoff * self._jitter()
                what = "network error" if response.status == NETWORK_ERROR else f"HTTP {response.status}"
                _log(f"{api_method}: {what}, retry {failures}/{self._transient_attempts - 1} in {wait:.1f}s")
                gate.extend(wait)
                continue

            if response.status >= 400 or payload is None:
                detail = response.body.decode("utf-8", "replace")[:300]
                raise PullError(f"{api_method} failed (HTTP {response.status}): {detail}")
            if payload.get("ok") is not True:
                error = payload.get("error") if isinstance(payload.get("error"), str) else "unknown_error"
                needed = payload.get("needed") if isinstance(payload.get("needed"), str) else None
                raise SlackApiError(api_method, error, needed)
            return payload

    def pages(
        self, api_method: str, params: dict, field: str, cursor: str = ""
    ) -> Iterator[tuple[list[dict], str]]:
        """Yield (items, next_cursor) per page, following response_metadata.next_cursor.

        Starts from ``cursor`` (to resume). next_cursor is "" on the last page.
        There is no page ceiling; a cursor seen twice stops with an error.
        """
        seen: set[str] = set()
        while True:
            query = dict(params, limit=self.page_limit(api_method))
            if cursor:
                if cursor in seen:
                    raise PullError(f"{api_method} pagination repeated its cursor")
                seen.add(cursor)
                query["cursor"] = cursor
            page = self.call(api_method, query)
            items = page.get(field)
            if not isinstance(items, list):
                raise PullError(f"{api_method} had no {field} list")
            for item in items:
                if not isinstance(item, dict):
                    raise PullError(f"{api_method} returned a non-object in {field}")
            meta = page.get("response_metadata")
            following = meta.get("next_cursor") if isinstance(meta, dict) else None
            following = following if isinstance(following, str) else ""
            yield items, following
            if not following:
                return
            cursor = following

    def paged(self, api_method: str, params: dict, field: str) -> Iterator[dict]:
        """Every item of every page, unchanged."""
        for items, _cursor in self.pages(api_method, params, field):
            yield from items


def _ts_key(message: dict) -> float:
    try:
        return float(message.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _with_channel(message: dict, channel_id: str) -> dict:
    if "channel" not in message:
        message["channel"] = channel_id
    return message


def _is_thread_parent(message: dict) -> bool:
    reply_count = message.get("reply_count")
    ts = message.get("ts")
    return isinstance(reply_count, int) and reply_count > 0 and isinstance(ts, str) and message.get("thread_ts") == ts


def order_channel(messages: Iterable[dict]) -> list[dict]:
    """Top-level messages oldest first, each followed by its replies.

    A message seen twice (a re-read page after a resume, or a reply also
    broadcast to the channel) is kept once, at its first copy. A reply whose
    parent is not in the set stays in time order among the top level.
    """
    unique: dict[str, dict] = {}
    unkeyed: list[dict] = []
    for message in messages:
        ts = message.get("ts")
        if isinstance(ts, str):
            unique.setdefault(ts, message)
        else:
            unkeyed.append(message)
    replies: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for ts, message in unique.items():
        thread_ts = message.get("thread_ts")
        if isinstance(thread_ts, str) and thread_ts != ts and thread_ts in unique:
            replies.setdefault(thread_ts, []).append(message)
        else:
            roots.append(message)
    roots.extend(unkeyed)
    roots.sort(key=_ts_key)
    ordered: list[dict] = []
    for root in roots:
        ordered.append(root)
        ordered.extend(sorted(replies.get(root.get("ts"), []), key=_ts_key))
    return ordered


class MemoryStore:
    """Holds one channel's raw messages in memory. No resume."""

    def __init__(self) -> None:
        self.state = {"phase": "history", "cursor": "", "parents_done": 0}
        self._messages: list[dict] = []

    def append(self, messages: list[dict]) -> None:
        self._messages.extend(messages)

    def messages(self) -> Iterator[dict]:
        return iter(self._messages)

    def save(self) -> None:
        return None


class Checkpoint:
    """Resume state for one --output run, saved atomically after every page."""

    def __init__(self, path: Path, data: dict) -> None:
        self.path = path
        self.data = data
        self._lock = threading.Lock()

    @classmethod
    def fresh(cls, path: Path, options: dict) -> Checkpoint:
        return cls(path, {"version": CHECKPOINT_VERSION, "team_id": None, "options": options, "channels": {}})

    @classmethod
    def load(cls, path: Path) -> Checkpoint:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PullError(f"could not read checkpoint {path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
            raise PullError(f"checkpoint {path} is from an incompatible version; delete it to start over")
        data.setdefault("channels", {})
        return cls(path, data)

    def channel_state(self, channel_id: str) -> dict:
        with self._lock:
            return self.data["channels"].setdefault(
                channel_id, {"phase": "history", "cursor": "", "parents_done": 0}
            )

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)


class FileStore:
    """One channel's raw messages in a part file, appended page by page."""

    def __init__(self, path: Path, checkpoint: Checkpoint, channel_id: str) -> None:
        self.path = path
        self.checkpoint = checkpoint
        self.state = checkpoint.channel_state(channel_id)

    def append(self, messages: list[dict]) -> None:
        if not messages:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            for message in messages:
                handle.write(json.dumps(message, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def messages(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line cut short by a crash; its page is re-read
                if isinstance(message, dict):
                    yield message

    def save(self) -> None:
        self.checkpoint.save()


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
        _log("the bot is not a member of any channel yet; run /invite @Ember in a channel to include its history")
    return members


def read_channel(client: SlackClient, channel: dict, oldest: str | None, store) -> None:
    """Read one channel into ``store``, saving progress after every page.

    Phase "history" pages through conversations.history from the saved
    cursor. Phase "replies" reads each thread parent's replies, resuming at
    the saved parent index. Phase "done" means nothing is left to read.
    """
    channel_id = channel["id"]
    state = store.state
    if state["phase"] == "history":
        params = {"channel": channel_id}
        if oldest is not None:
            params["oldest"] = oldest
        pages = 0
        try:
            for items, next_cursor in client.pages("conversations.history", params, "messages", state["cursor"]):
                store.append([_with_channel(message, channel_id) for message in items])
                state["cursor"] = next_cursor
                store.save()
                pages += 1
                if pages % 10 == 0:
                    _log(f"{channel_id}: {pages} history pages read")
        except SlackApiError as exc:
            if exc.error in CHANNEL_SKIP_ERRORS:
                _log(f"skipped {channel_id}: {exc.error}")
                state.update(phase="done", cursor="")
                store.save()
                return
            raise
        state.update(phase="replies", cursor="", parents_done=0)
        store.save()

    if state["phase"] == "replies":
        parents = sorted({m["ts"] for m in store.messages() if _is_thread_parent(m)}, key=float)
        for index in range(state["parents_done"], len(parents)):
            parent_ts = parents[index]
            try:
                replies = list(client.paged("conversations.replies", {"channel": channel_id, "ts": parent_ts}, "messages"))
            except SlackApiError as exc:
                if exc.error not in CHANNEL_SKIP_ERRORS and exc.error != "thread_not_found":
                    raise
                _log(f"skipped thread {channel_id}/{parent_ts}: {exc.error}")
                replies = []
            store.append([_with_channel(r, channel_id) for r in replies if r.get("ts") != parent_ts])
            state["parents_done"] = index + 1
            store.save()
            if (index + 1) % 50 == 0:
                _log(f"{channel_id}: {index + 1}/{len(parents)} threads read")
        state["phase"] = "done"
        store.save()


def channel_messages(client: SlackClient, channel: dict, oldest: str | None) -> list[dict]:
    """Top-level messages and their thread replies, oldest first, in memory."""
    store = MemoryStore()
    read_channel(client, channel, oldest, store)
    return order_channel(store.messages())


def _workspace(client: SlackClient, only_channel: str | None, include_archived: bool) -> tuple[dict, list[dict], list[dict]]:
    team_page = client.call("team.info")
    team = team_page.get("team")
    if not isinstance(team, dict):
        raise PullError("team.info had no team object")
    users = list(client.paged("users.list", {}, "members"))
    channels = select_channels(client, only_channel, include_archived=include_archived)
    return team, users, channels


def collect(
    client: SlackClient,
    *,
    only_channel: str | None = None,
    oldest: str | None = None,
    include_archived: bool = False,
    workers: int = DEFAULT_CONCURRENCY,
) -> list[dict]:
    """Raw objects in output order: team, users, then each channel followed
    by its messages and replies. In memory; no resume."""
    team, users, channels = _workspace(client, only_channel, include_archived)
    per_channel = map_ordered(lambda channel: channel_messages(client, channel, oldest), channels, workers)
    bodies: list[dict] = [team]
    bodies.extend(users)
    for channel, messages in zip(channels, per_channel):
        bodies.append(channel)
        bodies.extend(messages)
    return bodies


def make_client(
    token: str,
    exchange,
    *,
    rate_mode: str = DEFAULT_RATE_MODE,
    gates: MethodGates | None = None,
    jitter: Callable[[], float] | None = None,
) -> SlackClient:
    if gates is None:
        gates = MethodGates(rates=rate_table(rate_mode))
    return SlackClient(token, exchange, gates, page_limits=page_limits(rate_mode), jitter=jitter)


def pull(
    token: str,
    exchange,
    *,
    only_channel: str | None = None,
    oldest: str | None = None,
    include_archived: bool = False,
    workers: int = DEFAULT_CONCURRENCY,
    gates: MethodGates | None = None,
    rate_mode: str = DEFAULT_RATE_MODE,
    jitter: Callable[[], float] | None = None,
) -> Iterator[dict]:
    """In-memory pull. Yields envelopes in output order."""
    client = make_client(token, exchange, rate_mode=rate_mode, gates=gates, jitter=jitter)
    bodies = collect(
        client,
        only_channel=only_channel,
        oldest=oldest,
        include_archived=include_archived,
        workers=workers,
    )
    for body in bodies:
        yield wrap_slack(body)


def checkpoint_path(output: Path) -> Path:
    return Path(str(output) + ".checkpoint.json")


def parts_dir(output: Path) -> Path:
    return Path(str(output) + ".parts")


def pull_to_file(
    token: str,
    exchange,
    output: Path,
    *,
    resume: bool = False,
    only_channel: str | None = None,
    oldest: str | None = None,
    include_archived: bool = False,
    workers: int = DEFAULT_CONCURRENCY,
    gates: MethodGates | None = None,
    rate_mode: str = DEFAULT_RATE_MODE,
    jitter: Callable[[], float] | None = None,
) -> int:
    """Durable pull into ``output``. Returns the number of envelopes written.

    Progress is saved after every page. With ``resume`` an existing
    checkpoint for the same options is continued; without one the run
    starts fresh. Without ``resume`` an existing checkpoint is an error, so
    two different runs never mix.
    """
    client = make_client(token, exchange, rate_mode=rate_mode, gates=gates, jitter=jitter)
    options = {"channel": only_channel, "oldest": oldest, "include_archived": include_archived}
    cp_path = checkpoint_path(output)
    parts = parts_dir(output)

    if cp_path.exists():
        if not resume:
            raise PullError(
                f"a checkpoint from an unfinished run exists ({cp_path}). "
                "Re-run with --resume to continue it, or delete it and the .parts folder to start over."
            )
        checkpoint = Checkpoint.load(cp_path)
        if checkpoint.data.get("options") != options:
            raise PullError(
                f"the checkpoint {cp_path} was made with different options "
                f"({checkpoint.data.get('options')}); use the same --channel/--since/--include-archived, "
                "or delete it to start over"
            )
        _log(f"resuming from {cp_path}")
    else:
        if parts.exists():
            shutil.rmtree(parts)
        checkpoint = Checkpoint.fresh(cp_path, options)
    parts.mkdir(parents=True, exist_ok=True)

    team, users, channels = _workspace(client, only_channel, include_archived)
    recorded = checkpoint.data.get("team_id")
    if recorded and recorded != team.get("id"):
        raise PullError(
            f"the checkpoint belongs to workspace {recorded} but this token is for {team.get('id')}; "
            "delete the checkpoint to start over"
        )
    checkpoint.data["team_id"] = team.get("id")
    checkpoint.save()

    def run(channel: dict) -> None:
        store = FileStore(parts / f"{channel['id']}.jsonl", checkpoint, channel["id"])
        if store.state["phase"] != "done":
            read_channel(client, channel, oldest, store)

    map_ordered(run, channels, workers)

    tmp = output.with_name(output.name + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as handle:
        for body in [team, *users]:
            handle.write(json.dumps(wrap_slack(body), separators=(",", ":")) + "\n")
            count += 1
        for channel in channels:
            handle.write(json.dumps(wrap_slack(channel), separators=(",", ":")) + "\n")
            count += 1
            store = FileStore(parts / f"{channel['id']}.jsonl", checkpoint, channel["id"])
            for message in order_channel(store.messages()):
                handle.write(json.dumps(wrap_slack(message), separators=(",", ":")) + "\n")
                count += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, output)
    cp_path.unlink(missing_ok=True)
    shutil.rmtree(parts, ignore_errors=True)
    return count


def emit_jsonl(envelopes, stream) -> int:
    count = 0
    for envelope in envelopes:
        stream.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        count += 1
    stream.flush()
    return count


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


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


def render_summary(envelopes: Iterable[dict]) -> str:
    """Glance lines for a human. Does not replace or rewrite the JSONL intake."""
    bodies = [env.get("body") for env in envelopes if isinstance(env.get("body"), dict)]
    return _render_bodies(lambda: iter(bodies))


def render_summary_file(path: Path) -> str:
    """Same as render_summary, reading the JSONL twice instead of holding it."""

    def bodies() -> Iterator[dict]:
        for env in read_jsonl(path):
            body = env.get("body")
            if isinstance(body, dict):
                yield body

    return _render_bodies(bodies)


def _render_bodies(bodies: Callable[[], Iterator[dict]]) -> str:
    names: dict = {}
    channel_names: dict = {}
    for body in bodies():
        kind = classify(body)
        if kind == "user" and "id" in body:
            names[body["id"]] = _user_name(body)
        elif kind == "channel" and "id" in body:
            channel_names[body["id"]] = body.get("name") or body["id"]
    title = "Slack"
    groups: dict[str, list[str]] = {kind: [] for kind, _heading in SUMMARY_SECTIONS}
    for body in bodies():
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
        help="Write one-line EMBER-39 JSONL here instead of stdout. Saves progress after every page "
        "(<output>.checkpoint.json) so --resume can continue. Also writes <output>.readable.md unless --no-readable.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an unfinished --output run from its checkpoint (starts fresh if there is none).",
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
    parser.add_argument(
        "--rate-mode",
        choices=RATE_MODES,
        default=None,
        help="internal (default; Slack's normal tiers) or non_marketplace (history and replies at 1/min, "
        "15 per page). Defaults to SLACK_RATE_MODE.",
    )
    args = parser.parse_args(argv)
    if args.no_readable and args.readable:
        print("Use only one of --readable or --no-readable.", file=sys.stderr)
        return 2
    if args.readable == "-" and not args.output:
        print("--readable - needs --output so JSONL is not mixed into stdout.", file=sys.stderr)
        return 2
    if args.resume and not args.output:
        print("--resume needs --output: progress is only saved for a file.", file=sys.stderr)
        return 2

    try:
        token = bot_token()
        channel = resolve_channel(args.channel)
        oldest = parse_since(args.since)
        workers = resolve_concurrency(args.concurrency)
        rate_mode = resolve_rate_mode(args.rate_mode)
        options = dict(
            only_channel=channel,
            oldest=oldest,
            include_archived=args.include_archived,
            workers=workers,
            rate_mode=rate_mode,
        )
        if args.output:
            output = Path(args.output)
            count = pull_to_file(token, urllib_exchange, output, resume=args.resume, **options)
            print(f"wrote {count} envelopes to {output}", file=sys.stderr)
            summary = None if args.no_readable else render_summary_file(output)
        else:
            envelopes = list(pull(token, urllib_exchange, **options))
            count = emit_jsonl(envelopes, sys.stdout)
            print(f"wrote {count} envelopes", file=sys.stderr)
            summary = None if args.no_readable else render_summary(envelopes)
        if summary is not None:
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
