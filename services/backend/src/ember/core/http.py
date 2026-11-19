"""Calling a source's REST API: one exchange, retries, rate limits, and what a final response means.

The rules are the old backfill CLIs' (services/<source>-ingestion/pull_test.py, removed in EMBER-160; github-webhooks.md and
jira-webhooks.md, "Rate limits"). Requests are blocking urllib calls run in a thread, so the exchange
is one plain function tests replace. `Limits` is how an API says "slow down": a 429 by default, which
is Jira's rule; GitHub's subclass adds its 403 limits. A final response becomes the runtime's failure
classes (core/errors.py): a rate limit is Throttled, a 5xx, 401 or network error Transient, and any
other 4xx Permanent.
"""

from __future__ import annotations

import http.client
import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

from ember.core.errors import Permanent, Throttled, Transient

LOG = logging.getLogger("ember.http")

USER_AGENT = "ember-backend"
MAX_BACKOFF_WAIT = 120.0  # delays we invent: a 5xx, a network error, a 429 with no server wait
MAX_SERVER_WAIT = 3600.0  # Retry-After and reset times
# Rate-limit sleep one call may spend in its thread before it gives up as Throttled. Longer waits are
# the queue's: the row is set aside and its transaction released, instead of holding both
THROTTLE_BUDGET = 10.0
TRANSIENT_ATTEMPTS = 5
TRANSIENT_STATUSES = {500, 502, 503, 504}
RATE_LIMIT_FLOOR = 50  # pause at or under this many remaining requests
_POLICY_RE = re.compile(r'"([^"]*)"([^,]*)')
Exchange = Callable[[str, str, dict[str, str], bytes | None], "Response"]


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def lowered(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}


def urllib_exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    """One HTTP exchange. A network failure comes back as 503, so the same backoff retries it."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Response(503, {}, f"network error: {reason}".encode("utf-8", "replace"))


# ---------- reading the rate-limit headers ----------

def _as_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _header_float(lowered: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = _as_float(lowered.get(name))
        if value is not None:
            return value
    return None


def remaining(response: Response) -> float | None:
    return _header_float(response.lowered(), "x-ratelimit-remaining", "ratelimit-remaining")


def _seconds_until(raw: str | None, now: float) -> float | None:
    """Seconds until an epoch or ISO-8601 timestamp; negative once it has passed."""
    text = (raw or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp() - now
    value = _as_float(text)
    return None if value is None else value - now


def _policies(header: str) -> list[tuple[str, dict[str, str]]]:
    """A RateLimit / RateLimit-Policy header as (name, attributes)."""
    found = []
    for match in _POLICY_RE.finditer(header):
        attrs = {}
        for part in match.group(2).split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                attrs[key.strip().lower()] = value.strip().strip('"')
        found.append((match.group(1), attrs))
    return found


def _floor(limit: float | None) -> float:
    return float(RATE_LIMIT_FLOOR) if limit is None else float(min(RATE_LIMIT_FLOOR, max(1, int(limit) // 10)))


def _windows(response: Response, now: float) -> list[tuple[float, float | None, float]]:
    """(remaining, limit, seconds until reset) from the classic and the structured headers."""
    lowered = response.lowered()
    windows = []
    left = _header_float(lowered, "x-ratelimit-remaining", "ratelimit-remaining")
    limit = _header_float(lowered, "x-ratelimit-limit", "ratelimit-limit")
    reset = _seconds_until(lowered.get("x-ratelimit-reset") or lowered.get("ratelimit-reset"), now)
    if left is not None and reset is not None:
        windows.append((left, limit, reset))
    quotas = {}
    for name in ("ratelimit-policy", "beta-ratelimit-policy"):
        for policy, attrs in _policies(lowered.get(name, "")):
            quota = _as_float(attrs.get("q"))
            if quota is not None:
                quotas[policy] = quota
    for name in ("ratelimit", "beta-ratelimit"):
        for policy, attrs in _policies(lowered.get(name, "")):
            left, delta = _as_float(attrs.get("r")), _as_float(attrs.get("t"))
            if left is not None and delta is not None:
                windows.append((left, quotas.get(policy), delta))
    return windows


def throttle_seconds(response: Response, *, now: float | None = None) -> float | None:
    """Seconds to pause because the remaining quota is at or under the floor, else None."""
    clock = time.time() if now is None else now
    wait = None
    for left, limit, reset in _windows(response, clock):
        if left <= _floor(limit) and reset > 0:
            wait = max(wait or 0.0, min(reset, MAX_SERVER_WAIT))
    return wait


def _retry_after(lowered: dict[str, str], *, allow_beta: bool) -> float | None:
    raw = lowered.get("retry-after")
    if raw is None and allow_beta:
        raw = lowered.get("beta-retry-after")
    value = _as_float(raw)
    return None if value is None else max(0.0, value)


def _policy_wait(lowered: dict[str, str]) -> float | None:
    """The longest `t` of an exhausted (r=0) structured policy."""
    wait = None
    for name in ("ratelimit", "beta-ratelimit"):
        for _policy, attrs in _policies(lowered.get(name, "")):
            delta, left = _as_float(attrs.get("t")), _as_float(attrs.get("r"))
            if delta is not None and delta > 0 and left == 0:
                wait = delta if wait is None else max(wait, delta)
    return wait


def _server_wait(response: Response, now: float) -> float | None:
    """What the response asked us to wait. With the quota exhausted, the latest of its waits wins."""
    lowered = response.lowered()
    retry_after = _retry_after(lowered, allow_beta=response.status == 429)
    left = _header_float(lowered, "x-ratelimit-remaining", "ratelimit-remaining")
    reset = _seconds_until(lowered.get("x-ratelimit-reset") or lowered.get("ratelimit-reset"), now)
    policy = _policy_wait(lowered)
    # x-ratelimit-reset is on every response; it applies only when the primary quota is spent
    reset_applies = reset is not None and reset > 0 and (left is None or left == 0)
    candidates = [w for w in (retry_after, reset if reset_applies else None, policy) if w is not None]
    if not candidates:
        return None
    return max(candidates) if left == 0 or policy is not None else candidates[0]


def _backoff(attempt: int) -> float:
    return min(2.0 ** max(0, attempt), MAX_BACKOFF_WAIT) * random.uniform(0.5, 1.0)


class Limits:
    """How an API says "slow down". This default is a 429, Jira's rule: a 403 is a permission error."""

    name = "API"

    def is_rate_limited(self, response: Response) -> bool:
        return response.status == 429

    def backoff(self, response: Response, attempt: int) -> float:
        """The wait for a limit that named no delay of its own."""
        return _backoff(attempt)

    def rate_limit_wait(self, response: Response, attempt: int = 0, *, now: float | None = None) -> float:
        """Seconds before retrying a rate-limited response. A server-named delay is never jittered."""
        server = _server_wait(response, time.time() if now is None else now)
        if server is not None and server > 0:
            return min(server, MAX_SERVER_WAIT)
        return self.backoff(response, attempt)

    def pause(self, response: Response) -> float:
        """How long every caller of this bucket waits after `response`: its limit, else a low quota."""
        limited = self.rate_limit_wait(response) if self.is_rate_limited(response) else 0.0
        return max(limited, throttle_seconds(response) or 0.0)


def transient_wait(response: Response, attempt: int = 0) -> float:
    """A 5xx or network error: Retry-After, else 1, 2, 4 ... seconds times jitter."""
    server = _retry_after(response.lowered(), allow_beta=False)
    if server is not None and server > 0:
        return min(server, MAX_SERVER_WAIT)
    return _backoff(attempt)


def exchange_with_retry(exchange: Exchange, method: str, url: str, request_headers: dict[str, str],
                        body: bytes | None, *, limits: Limits, throttle_budget: float = THROTTLE_BUDGET,
                        sleep: Callable[[float], None] = time.sleep) -> Response:
    """Send one request, retrying 5xx and network errors and sleeping out short rate limits.

    Returns the final response, which may still be rate-limited (once `throttle_budget` is spent) or a 5xx.
    """
    throttled, failures, rate_attempt = 0.0, 0, 0
    while True:
        response = exchange(method, url, request_headers, body)
        if limits.is_rate_limited(response):
            wait = max(limits.rate_limit_wait(response, rate_attempt), 1.0)
            if throttled + wait > throttle_budget:
                return response
            throttled += wait
            rate_attempt += 1
            LOG.warning("%s asked to wait %.1fs (%s)", limits.name, wait, response.status)
            sleep(wait)
            continue
        if response.status in TRANSIENT_STATUSES:
            failures += 1
            if failures >= TRANSIENT_ATTEMPTS:
                return response
            wait = transient_wait(response, failures - 1)
            LOG.warning("%s HTTP %s, retry %d/%d in %.1fs", limits.name, response.status, failures,
                        TRANSIENT_ATTEMPTS - 1, wait)
            sleep(wait)
            continue
        return response


def failure(response: Response, what: str, limits: Limits) -> Exception:
    """The failure class a final error response means."""
    detail = f"{what} failed ({response.status})"
    if limits.is_rate_limited(response):
        return Throttled(detail, retry_after=limits.rate_limit_wait(response))
    if response.status in TRANSIENT_STATUSES or response.status == 401:  # 401: a token that just expired
        return Transient(detail)
    return Permanent(detail)  # 403 without a limit, 404, 410, 422: retrying won't help


def read_json(response: Response, what: str, limits: Limits):
    """The body of a successful response, or the failure class its status means."""
    if response.status < 400:
        return response.json()
    raise failure(response, what, limits)
