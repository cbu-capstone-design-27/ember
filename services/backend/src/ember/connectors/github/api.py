"""The GitHub REST calls enrich makes: an installation token, then pages of one resource.

The retry and rate-limit rules are services/github-ingestion/pull_test.py's, which the backfill CLI
still uses (github-webhooks.md, "Rate limits"). Requests are blocking urllib calls run in a thread,
so the exchange is one plain function tests replace. A final response becomes the runtime's
failure classes (core/errors.py): a rate limit is Throttled, a 5xx or network error Transient, and
any other 4xx Permanent.
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

import jwt

from ember.core.errors import Permanent, Throttled, Transient

LOG = logging.getLogger("ember.github")

API = "https://api.github.com"
USER_AGENT = "ember-backend"
MAX_PAGES = 200
MAX_BACKOFF_WAIT = 120.0  # delays we invent: a 5xx, a network error, a 429 with no server wait
MAX_SERVER_WAIT = 3600.0  # Retry-After and reset times; GitHub's primary window is an hour
# Rate-limit sleep one call may spend in its thread before it gives up as Throttled. Longer waits are
# the queue's: the row is set aside and its transaction released, instead of holding both
THROTTLE_BUDGET = 10.0
TRANSIENT_ATTEMPTS = 5
TRANSIENT_STATUSES = {500, 502, 503, 504}
RATE_LIMIT_FLOOR = 50  # pause at or under this many remaining requests
SECONDARY_RATE_LIMIT_WAIT = 60.0
_POLICY_RE = re.compile(r'"([^"]*)"([^,]*)')
Exchange = Callable[[str, str, dict[str, str], bytes | None], "Response"]


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


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


def app_jwt(app_id: str, private_key: str, *, now: int | None = None) -> str:
    """The GitHub App's own JWT (RS256), good for 10 minutes; GitHub allows 60 s of clock drift back."""
    issued = int(time.time()) if now is None else now
    return jwt.encode({"iat": issued - 60, "exp": issued + 9 * 60, "iss": str(app_id)}, private_key, algorithm="RS256")


def headers(token: str, *, json_body: bool = False) -> dict[str, str]:
    out = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
    if json_body:
        out["Content-Type"] = "application/json"
    return out


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start, end = part.find("<"), part.find(">")
            if start != -1 and end != -1:
                return part[start + 1 : end]
    return None


# ---------- rate limits (pull_test.py's rules) ----------

def _lower(response: Response) -> dict[str, str]:
    return {key.lower(): value for key, value in response.headers.items()}


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
    lowered = _lower(response)
    windows = []
    remaining = _header_float(lowered, "x-ratelimit-remaining", "ratelimit-remaining")
    limit = _header_float(lowered, "x-ratelimit-limit", "ratelimit-limit")
    reset = _seconds_until(lowered.get("x-ratelimit-reset") or lowered.get("ratelimit-reset"), now)
    if remaining is not None and reset is not None:
        windows.append((remaining, limit, reset))
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
    for remaining, limit, reset in _windows(response, clock):
        if remaining <= _floor(limit) and reset > 0:
            wait = max(wait or 0.0, min(reset, MAX_SERVER_WAIT))
    return wait


def _retry_after(lowered: dict[str, str], *, allow_beta: bool) -> float | None:
    raw = lowered.get("retry-after")
    if raw is None and allow_beta:
        raw = lowered.get("beta-retry-after")
    value = _as_float(raw)
    return None if value is None else max(0.0, value)


def _is_secondary(response: Response) -> bool:
    text = response.body.decode("utf-8", "replace").lower()
    return "secondary rate limit" in text or "abuse detection" in text


def is_rate_limited(response: Response) -> bool:
    """A primary or secondary limit, not a permission 403."""
    if response.status == 429:
        return True
    if response.status != 403:
        return False
    lowered = _lower(response)
    if lowered.get("retry-after"):
        return True
    if _header_float(lowered, "x-ratelimit-remaining", "ratelimit-remaining") == 0:
        return True
    return _is_secondary(response)


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
    lowered = _lower(response)
    retry_after = _retry_after(lowered, allow_beta=response.status == 429)
    remaining = _header_float(lowered, "x-ratelimit-remaining", "ratelimit-remaining")
    reset = _seconds_until(lowered.get("x-ratelimit-reset") or lowered.get("ratelimit-reset"), now)
    policy = _policy_wait(lowered)
    # x-ratelimit-reset is on every response; it applies only when the primary quota is spent
    reset_applies = reset is not None and reset > 0 and (remaining is None or remaining == 0)
    candidates = [w for w in (retry_after, reset if reset_applies else None, policy) if w is not None]
    if not candidates:
        return None
    return max(candidates) if remaining == 0 or policy is not None else candidates[0]


def rate_limit_wait(response: Response, attempt: int = 0, *, now: float | None = None) -> float:
    """Seconds before retrying a rate-limited response. A server-named delay is never jittered."""
    server = _server_wait(response, time.time() if now is None else now)
    if server is not None and server > 0:
        return min(server, MAX_SERVER_WAIT)
    if _is_secondary(response):
        return min(SECONDARY_RATE_LIMIT_WAIT * 2 ** max(0, attempt), MAX_BACKOFF_WAIT)
    return min(2.0 ** max(0, attempt), MAX_BACKOFF_WAIT) * random.uniform(0.5, 1.0)


def transient_wait(response: Response, attempt: int = 0) -> float:
    """A 5xx or network error: Retry-After, else 1, 2, 4 ... seconds times jitter."""
    server = _retry_after(_lower(response), allow_beta=False)
    if server is not None and server > 0:
        return min(server, MAX_SERVER_WAIT)
    return min(2.0 ** max(0, attempt), MAX_BACKOFF_WAIT) * random.uniform(0.5, 1.0)


def exchange_with_retry(exchange: Exchange, method: str, url: str, request_headers: dict[str, str],
                        body: bytes | None, *, sleep: Callable[[float], None] = time.sleep) -> Response:
    """Send one request, retrying 5xx and network errors and sleeping out short rate limits.

    Returns the final response, which may still be rate-limited (once THROTTLE_BUDGET is spent) or a 5xx.
    """
    throttled, failures, rate_attempt = 0.0, 0, 0
    while True:
        response = exchange(method, url, request_headers, body)
        if is_rate_limited(response):
            wait = max(rate_limit_wait(response, rate_attempt), 1.0)
            if throttled + wait > THROTTLE_BUDGET:
                return response
            throttled += wait
            rate_attempt += 1
            LOG.warning("GitHub asked to wait %.1fs (%s)", wait, response.status)
            sleep(wait)
            continue
        if response.status in TRANSIENT_STATUSES:
            failures += 1
            if failures >= TRANSIENT_ATTEMPTS:
                return response
            wait = transient_wait(response, failures - 1)
            LOG.warning("GitHub HTTP %s, retry %d/%d in %.1fs", response.status, failures, TRANSIENT_ATTEMPTS - 1, wait)
            sleep(wait)
            continue
        return response


def read_json(response: Response, what: str):
    """The body of a successful response, or the failure class its status means."""
    if response.status < 400:
        return response.json()
    detail = f"{what} failed ({response.status})"
    if is_rate_limited(response):
        raise Throttled(detail, retry_after=rate_limit_wait(response))
    if response.status in TRANSIENT_STATUSES or response.status == 401:  # 401: a token that just expired
        raise Transient(detail)
    raise Permanent(detail)  # 403 without a limit, 404, 410, 422: retrying won't help


def installation_token(jwt_token: str, repo: str, exchange: Exchange) -> str:
    """Look up the repo's installation with the App JWT, then mint that installation a token."""
    found = exchange_with_retry(exchange, "GET", f"{API}/repos/{repo}/installation", headers(jwt_token), None)
    installation_id = read_json(found, f"installation lookup for {repo}").get("id")
    if not isinstance(installation_id, int):
        raise Permanent(f"installation lookup for {repo} returned no id")
    minted = exchange_with_retry(exchange, "POST", f"{API}/app/installations/{installation_id}/access_tokens",
                                 headers(jwt_token, json_body=True), b"{}")
    token = read_json(minted, "installation token").get("token")
    if not isinstance(token, str) or not token:
        raise Transient("installation token response had no token")
    return token
