"""Local Jira pull for one test project.

Reads a project with HTTP basic auth (email + API token) and writes one
EMBER-39 envelope per discrete JSON object. Pull requests do not exist
in Jira. Attachment file bytes are not downloaded.

Machine intake is JSONL, one object per line:
  {"type":"jira","body":<opaque raw Jira object>}
Stdout is that JSONL. --output FILE writes the same JSONL and, by default,
a glance summary at FILE.readable.md (issue key, summary, and a short
description; comment author and a short body). The JSONL line stays the
full raw object. Search pages stay serial. Detail GETs run DEFAULT_CONCURRENCY at
a time (--concurrency or JIRA_PULL_CONCURRENCY). One shared gate pauses
when the remaining quota is low, waits out Retry-After or a rate-limit
reset, and retries 5xx and network errors with jittered backoff.
--readable PATH chooses the summary file. --no-readable skips it.
--readable - prints the summary on stdout and requires --output so the
JSONL stays a file. Progress goes to stderr.

The webhook receiver does not use this script. JIRA_TEST_PROJECT is the
test target only (default EMBER). Nothing here filters live deliveries.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import random
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
from urllib.parse import quote, urlencode

from envelope import wrap_jira

USER_AGENT = "ember-jira-ingestion"
MAX_PAGES = 50
PAGE_SIZE = 100
DEFAULT_CONCURRENCY = 32
MAX_CONCURRENCY = 64
# Delays this connector invents (5xx, network, a 429 with no server wait).
MAX_BACKOFF_WAIT = 120.0
# Retry-After and reset timestamps. A Jira quota 429 can name a wait of
# most of an hour, and the docs say not to call again before that.
MAX_SERVER_WAIT = 3600.0
# Total rate-limit sleep one call may accumulate before it fails.
THROTTLE_BUDGET = 3600.0
TRANSIENT_ATTEMPTS = 5
TRANSIENT_STATUSES = {500, 502, 503, 504}
# Pause every worker at or below this many remaining requests. Default
# concurrency (32) is under it, so requests already in flight can finish.
# The floor shrinks for small limits (a per-second burst bucket) so a
# 100-request window does not pause on every response.
RATE_LIMIT_FLOOR = 50
_POLICY_RE = re.compile(r'"([^"]*)"([^,]*)')
DEFAULT_PROJECT = "EMBER"
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


class PullError(Exception):
    pass


def site_origin(base: str) -> str:
    """https origin with no userinfo and no path."""
    text = base.strip().rstrip("/")
    if not text.startswith("https://"):
        raise PullError("JIRA_BASE_URL must be an https origin, like https://<site>")
    rest = text[len("https://") :]
    if not rest or "/" in rest or "@" in rest or any(ch.isspace() for ch in rest):
        raise PullError("JIRA_BASE_URL must be an https origin with no path, like https://<site>")
    return text


def resolve_project(cli_value: str | None) -> str:
    """CLI wins, then JIRA_TEST_PROJECT, then DEFAULT_PROJECT."""
    if cli_value is None:
        raw = os.environ.get("JIRA_TEST_PROJECT", "").strip()
        project = raw or DEFAULT_PROJECT
    else:
        project = cli_value.strip()
    if not PROJECT_KEY.fullmatch(project):
        raise PullError(
            "JIRA_TEST_PROJECT must be a Jira project key such as EMBER "
            "(uppercase letters and digits, 2-10 characters)."
        )
    return project


def resolve_concurrency(cli_value: int | None) -> int:
    """CLI wins, then JIRA_PULL_CONCURRENCY, then DEFAULT_CONCURRENCY."""
    if cli_value is None:
        raw = os.environ.get("JIRA_PULL_CONCURRENCY", "").strip()
        if not raw:
            return DEFAULT_CONCURRENCY
        try:
            cli_value = int(raw)
        except ValueError as exc:
            raise PullError("JIRA_PULL_CONCURRENCY must be an integer") from exc
    if cli_value < 1 or cli_value > MAX_CONCURRENCY:
        raise PullError(f"concurrency must be from 1 to {MAX_CONCURRENCY}")
    return cli_value


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


Jitter = Callable[[], float]


def _default_jitter() -> float:
    return random.uniform(0.5, 1.0)


def _wait_text(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"


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


def _lower_headers(response: Response) -> dict[str, str]:
    return {key.lower(): value for key, value in response.headers.items()}


def _as_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _header_float(headers: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = _as_float(headers.get(name))
        if value is not None:
            return value
    return None


def seconds_until_absolute(raw: str | None, now: float) -> float | None:
    """Seconds until an epoch or ISO-8601 timestamp. Negative when it has passed."""
    if raw is None:
        return None
    text = raw.strip()
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
    if value is None:
        return None
    return value - now


def _policies(header: str) -> list[tuple[str, dict[str, str]]]:
    """Parse a RateLimit / RateLimit-Policy header into (name, attributes)."""
    found: list[tuple[str, dict[str, str]]] = []
    for match in _POLICY_RE.finditer(header):
        attrs: dict[str, str] = {}
        for part in match.group(2).split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            attrs[key.strip().lower()] = value.strip().strip('"')
        found.append((match.group(1), attrs))
    return found


def quota_floor(limit: float | None) -> float:
    """Remaining count at or below which every worker pauses."""
    if limit is None:
        return float(RATE_LIMIT_FLOOR)
    return float(min(RATE_LIMIT_FLOOR, max(1, int(limit) // 10)))


def _quota_windows(response: Response, now: float) -> list[tuple[float, float | None, float]]:
    """(remaining, limit, seconds-until-reset) from classic and structured headers."""
    headers = _lower_headers(response)
    windows: list[tuple[float, float | None, float]] = []
    remaining = _header_float(headers, "x-ratelimit-remaining", "ratelimit-remaining")
    limit = _header_float(headers, "x-ratelimit-limit", "ratelimit-limit")
    reset_raw = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
    reset_wait = seconds_until_absolute(reset_raw, now)
    if remaining is not None and reset_wait is not None:
        windows.append((remaining, limit, reset_wait))

    policy_limits: dict[str, float] = {}
    for name in ("ratelimit-policy", "beta-ratelimit-policy"):
        raw = headers.get(name)
        if not raw or '"' not in raw:
            continue
        for policy, attrs in _policies(raw):
            quota = _as_float(attrs.get("q"))
            if quota is not None:
                policy_limits[policy] = quota
    for name in ("ratelimit", "beta-ratelimit"):
        raw = headers.get(name)
        if not raw or '"' not in raw:
            continue
        for policy, attrs in _policies(raw):
            left = _as_float(attrs.get("r"))
            delta = _as_float(attrs.get("t"))
            if left is None or delta is None:
                continue
            windows.append((left, policy_limits.get(policy), delta))
    return windows


def throttle_seconds(response: Response, *, now: float | None = None) -> float | None:
    """Seconds to pause because the remaining quota is at or under the floor.

    Needs both a remaining count and a reset. A healthy remaining count, or a
    remaining count with no reset, does not pause. ``X-RateLimit-NearLimit``
    is not a trigger: on an hourly quota it means 20% remains, which would
    stall a backfill that is still inside its budget.
    """
    clock = time.time() if now is None else now
    wait: float | None = None
    for remaining, limit, reset_wait in _quota_windows(response, clock):
        if remaining > quota_floor(limit) or reset_wait <= 0:
            continue
        capped = min(reset_wait, MAX_SERVER_WAIT)
        if wait is None or capped > wait:
            wait = capped
    return wait


def _retry_after_header(headers: dict[str, str], *, allow_beta: bool) -> float | None:
    raw = headers.get("retry-after")
    if raw is None and allow_beta:
        raw = headers.get("beta-retry-after")
    value = _as_float(raw)
    if value is None:
        return None
    return max(0.0, value)


def _policy_wait(headers: dict[str, str]) -> float | None:
    """Longest structured ``t`` (seconds until reset) for an exhausted policy."""
    wait: float | None = None
    for name in ("ratelimit", "beta-ratelimit"):
        raw = headers.get(name)
        if not raw or '"' not in raw:
            continue
        for _policy, attrs in _policies(raw):
            delta = _as_float(attrs.get("t"))
            left = _as_float(attrs.get("r"))
            if delta is None or delta <= 0 or left != 0:
                continue
            wait = delta if wait is None else max(wait, delta)
    return wait


def is_rate_limited(response: Response) -> bool:
    """Jira signals a limit with 429. A 403 is permission, not a throttle."""
    return response.status == 429


def _server_wait(response: Response, now: float) -> float | None:
    """Seconds the response itself asked us to wait, or None if it didn't.

    When remaining is 0, the later of Retry-After and the reset wins, so the
    connector does not call again while that window is still closed.
    """
    headers = _lower_headers(response)
    retry_after = _retry_after_header(headers, allow_beta=response.status == 429)
    remaining = _header_float(headers, "x-ratelimit-remaining", "ratelimit-remaining")
    reset_raw = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
    reset_wait = seconds_until_absolute(reset_raw, now)
    policy_wait = _policy_wait(headers)
    # A reset timestamp is on successful responses too. Use it as a retry
    # delay only when the quota is exhausted, or when no remaining count
    # was sent with it.
    reset_applies = reset_wait is not None and reset_wait > 0 and (remaining is None or remaining == 0)
    exhausted = remaining == 0 or policy_wait is not None

    candidates: list[float] = []
    if retry_after is not None:
        candidates.append(retry_after)
    if reset_applies:
        candidates.append(reset_wait)
    if policy_wait is not None:
        candidates.append(policy_wait)
    if not candidates:
        return None
    if exhausted:
        return max(candidates)
    return candidates[0]


def rate_limit_wait(
    response: Response,
    attempt: int = 0,
    *,
    now: float | None = None,
    jitter: Jitter | None = None,
) -> float:
    """Seconds to wait before retrying a 429. A server-supplied delay is not
    jittered. A 429 with no delay backs off 1, 2, 4, ... times jitter."""
    clock = time.time() if now is None else now
    server = _server_wait(response, clock)
    if server is not None and server > 0:
        return min(server, MAX_SERVER_WAIT)
    scale = jitter or _default_jitter
    base = min(float(2 ** max(0, attempt)), MAX_BACKOFF_WAIT)
    return min(base * scale(), MAX_BACKOFF_WAIT)


def transient_wait(response: Response, attempt: int = 0, *, jitter: Jitter | None = None) -> float:
    """Seconds to wait on a 5xx or network error. Retry-After wins; otherwise
    1, 2, 4, ... seconds times jitter in [0.5, 1], capped at MAX_BACKOFF_WAIT."""
    headers = _lower_headers(response)
    server = _retry_after_header(headers, allow_beta=False)
    if server is not None and server > 0:
        return min(server, MAX_SERVER_WAIT)
    scale = jitter or _default_jitter
    base = min(float(2 ** max(0, attempt)), MAX_BACKOFF_WAIT)
    return min(base * scale(), MAX_BACKOFF_WAIT)


def retry_after_seconds(
    response: Response,
    attempt: int = 0,
    *,
    now: float | None = None,
    jitter: Jitter | None = None,
) -> float | None:
    """Seconds to wait before retrying, or None when the response is final.

    Retry-After and a reset timestamp win, without jitter. A 429 or 5xx
    without either backs off. Pass ``jitter`` (for example ``lambda: 1``)
    to make that backoff deterministic.
    """
    if is_rate_limited(response):
        return rate_limit_wait(response, attempt, now=now, jitter=jitter)
    if response.status in TRANSIENT_STATUSES:
        return transient_wait(response, attempt, jitter=jitter)
    return None


def exchange_with_retry(
    exchange,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    *,
    attempts: int = TRANSIENT_ATTEMPTS,
    throttle_budget: float = THROTTLE_BUDGET,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    jitter: Jitter | None = None,
    gate: RateLimitGate | None = None,
) -> Response:
    """Send one request. Low quota pauses every worker. A 429 retries until
    ``throttle_budget`` seconds have been waited. 5xx and network errors
    retry ``attempts`` times with jittered backoff.

    ``gate`` is shared by every worker in one pull. A throttle extends it so
    the next request from any worker waits out the same window.
    """
    scale = jitter or _default_jitter
    throttled = 0.0
    failures = 0
    rate_attempt = 0
    response: Response | None = None
    while True:
        if gate is not None:
            gate.wait()
        response = exchange(method, url, headers, body)
        now = clock()
        pause = throttle_seconds(response, now=now)
        if pause is not None and gate is not None:
            gate.extend(pause)
            print(f"Jira quota low; pausing {_wait_text(pause)}", file=sys.stderr)
        if is_rate_limited(response):
            wait = rate_limit_wait(response, rate_attempt, now=now, jitter=scale)
            if wait <= 0:
                wait = 1.0
            if throttled + wait > throttle_budget:
                return response
            throttled += wait
            rate_attempt += 1
            if gate is not None:
                gate.extend(wait)
            print(f"Jira asked to wait {_wait_text(wait)} ({response.status}) {url}", file=sys.stderr)
            sleep(wait)
            continue
        if response.status in TRANSIENT_STATUSES:
            failures += 1
            if failures >= attempts:
                return response
            wait = transient_wait(response, failures - 1, jitter=scale)
            if gate is not None:
                gate.extend(wait)
            print(
                f"Jira HTTP {response.status}, retry {failures}/{attempts - 1} in {wait:.1f}s {url}",
                file=sys.stderr,
            )
            sleep(wait)
            continue
        return response


def map_ordered(fn, items: list, workers: int) -> list:
    """Run fn over items concurrently and return results in input order."""
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def urllib_exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    """One HTTP exchange. A network failure comes back as 503 so the same
    backoff retries it instead of ending a long backfill."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Response(503, {}, f"network error: {reason}".encode("utf-8", "replace"))


def _headers(email: str, api_token: str, *, json_body: bool = False) -> dict[str, str]:
    token = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _read_json(response: Response, what: str):
    if response.status >= 400:
        detail = response.body.decode("utf-8", "replace")[:500]
        raise PullError(f"{what} failed ({response.status}): {detail}")
    return response.json()


def iter_issue_keys(origin: str, project: str, email: str, api_token: str, exchange) -> Iterator[str]:
    """Pages of /rest/api/3/search/jql. The removed /rest/api/3/search is not used.

    Each page asks for keys only. The following GET /issue/{key} is the
    object that gets emitted, so the description is the full issue field.
    """
    url = f"{origin}/rest/api/3/search/jql"
    next_token: str | None = None
    pages = 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise PullError(f"stopped after {MAX_PAGES} search pages for project {project}")
        payload: dict = {
            "jql": f"project = {project} ORDER BY key ASC",
            "maxResults": PAGE_SIZE,
            "fields": ["key"],
        }
        if next_token:
            payload["nextPageToken"] = next_token
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = exchange(
            "POST",
            url,
            _headers(email, api_token, json_body=True),
            body,
        )
        page = _read_json(response, f"POST {url}")
        if not isinstance(page, dict):
            raise PullError(f"expected a JSON object from {url}")
        issues = page.get("issues")
        if not isinstance(issues, list):
            raise PullError(f"expected issues from {url}")
        for item in issues:
            if not isinstance(item, dict):
                raise PullError(f"expected objects in {url}")
            key = item.get("key")
            if not isinstance(key, str) or not ISSUE_KEY.fullmatch(key):
                raise PullError(f"search result had no issue key from {url}")
            yield key
        if page.get("isLast") is True:
            return
        token = page.get("nextPageToken")
        if not isinstance(token, str) or not token:
            if page.get("isLast") is False:
                raise PullError(f"search page for {project} was not last and had no nextPageToken")
            return
        if token == next_token:
            raise PullError(f"search pagination repeated nextPageToken for {project}")
        next_token = token


def _reject_binary(url: str) -> None:
    if "/attachment/content/" in url or "/attachment/thumbnail/" in url:
        raise PullError(f"refusing attachment bytes: {url}")


def fetch_json(url: str, email: str, api_token: str, exchange, *, optional: bool = False):
    """GET JSON. optional 403/404 returns None. The body is not rewritten."""
    _reject_binary(url)
    response = exchange("GET", url, _headers(email, api_token), None)
    if optional and response.status in (403, 404):
        print(f"skipped optional {response.status} {url}", file=sys.stderr)
        return None
    return _read_json(response, f"GET {url}")


def _as_dict(payload, url: str) -> dict:
    if not isinstance(payload, dict):
        raise PullError(f"expected a JSON object from {url}")
    return payload


def _as_dicts(payload, url: str) -> list[dict]:
    if not isinstance(payload, list):
        raise PullError(f"expected a JSON list from {url}")
    for item in payload:
        if not isinstance(item, dict):
            raise PullError(f"expected objects in {url}")
    return payload


def issue_url(origin: str, key: str) -> str:
    return f"{origin}/rest/api/3/issue/{key}?{urlencode({'fields': '*all'})}"


def paged_url(origin: str, path: str, start: int) -> str:
    return f"{origin}{path}?{urlencode({'startAt': start, 'maxResults': PAGE_SIZE})}"


def property_url(collection_url: str, key: str) -> str:
    return collection_url.rstrip("/") + "/" + quote(key, safe="")


def _bucket_total(issue: dict, field: str) -> int | None:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return None
    bucket = fields.get(field)
    if not isinstance(bucket, dict):
        return None
    total = bucket.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    return total


def _page_size(page: dict) -> int:
    size = page.get("maxResults")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        return PAGE_SIZE
    return size


def remaining_starts(page: dict, url: str) -> list[int]:
    """startAt values after the first page. Empty when that page is the last."""
    if page.get("isLast") is True:
        return []
    total = page.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise PullError(f"page had no total: {url}")
    size = _page_size(page)
    starts = list(range(size, total, size))
    if len(starts) + 1 > MAX_PAGES:
        raise PullError(f"stopped after {MAX_PAGES} pages for {url}")
    return starts


def _page_items(page: dict, field: str, url: str) -> list[dict]:
    return _as_dicts(page.get(field), url)


def _property_names(payload, url: str) -> list[str]:
    if payload is None:
        return []
    page = _as_dict(payload, url)
    keys = page.get("keys")
    if not isinstance(keys, list):
        raise PullError(f"expected keys from {url}")
    names: list[str] = []
    for item in keys:
        if not isinstance(item, dict):
            raise PullError(f"expected objects in {url}")
        name = item.get("key")
        if not isinstance(name, str) or not name:
            raise PullError(f"property list had no key from {url}")
        names.append(name)
    return names


def _issue_links(issue: dict) -> list[dict]:
    fields = issue.get("fields")
    if not isinstance(fields, dict) or "issuelinks" not in fields or fields.get("issuelinks") is None:
        return []
    return _as_dicts(fields.get("issuelinks"), f"issue {issue.get('key')} issuelinks")


def _attachment_ids(issue: dict) -> list[str]:
    fields = issue.get("fields")
    if not isinstance(fields, dict) or "attachment" not in fields or fields.get("attachment") is None:
        return []
    raw = fields.get("attachment")
    if not isinstance(raw, list):
        raise PullError(f"issue {issue.get('key')} attachment field was not a list")
    found: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PullError(f"issue {issue.get('key')} attachment was not an object")
        att_id = item.get("id")
        if isinstance(att_id, bool) or not isinstance(att_id, (str, int)) or att_id == "":
            raise PullError(f"issue {issue.get('key')} attachment had no id")
        found.append(str(att_id))
    return found


def changelog_ready(page: dict) -> bool:
    """True when a changelog page has an entry list.

    GET /rest/api/3/issue/{key}/changelog is a page bean. The entries are
    in `values`. `histories` is the older expand=changelog shape on the
    issue resource. The page is stored as returned; this does not rename keys.
    """
    return isinstance(page.get("values"), list) or isinstance(page.get("histories"), list)


def _fetch_page(url: str, email: str, api_token: str, exchange) -> dict:
    return _as_dict(fetch_json(url, email, api_token, exchange), url)


def _extend_pages(
    first: dict,
    build_url: Callable[[int], str],
    email: str,
    api_token: str,
    exchange,
    workers: int,
) -> list[dict]:
    first_url = build_url(0)
    pages = [first]
    starts = remaining_starts(first, first_url)
    if not starts:
        return pages
    more = map_ordered(
        lambda start: _fetch_page(build_url(start), email, api_token, exchange),
        starts,
        workers,
    )
    pages.extend(more)
    return pages


def _items_from_pages(pages: list[dict], field: str, build_url: Callable[[int], str]) -> list[dict]:
    items: list[dict] = []
    start = 0
    for page in pages:
        items.extend(_page_items(page, field, build_url(start)))
        start += _page_size(page)
    return items


class _Bundle:
    def __init__(self) -> None:
        self.links: list[dict] = []
        self.comments: list[dict] = []
        self.changelog: list[dict] = []
        self.worklogs: list[dict] = []
        self.attachments: list[dict] = []
        self.remote_links: list[dict] = []
        self.watchers: dict | None = None
        self.votes: dict | None = None
        self.properties: list[dict] = []


def _load_roots(origin: str, project: str, keys: list[str], email: str, api_token: str, exchange, workers: int):
    """Project record plus every issue. Search has already listed the keys."""
    project_url = f"{origin}/rest/api/3/project/{project}"
    components_url = f"{project_url}/components"
    statuses_url = f"{project_url}/statuses"
    properties_url = f"{project_url}/properties"
    version_url = lambda start: paged_url(origin, f"/rest/api/3/project/{project}/version", start)

    jobs: list[tuple] = [
        ("project", project_url),
        ("components", components_url),
        ("statuses", statuses_url),
        ("properties", properties_url),
        ("versions", version_url(0)),
    ]
    jobs.extend(("issue", issue_url(origin, key)) for key in keys)
    payloads = map_ordered(
        lambda job: fetch_json(job[1], email, api_token, exchange),
        jobs,
        workers,
    )
    by_label = {job[0] if job[0] != "issue" else job[1]: payload for job, payload in zip(jobs, payloads)}
    project_body = _as_dict(by_label["project"], project_url)
    components = _as_dicts(by_label["components"], components_url)
    statuses = _as_dicts(by_label["statuses"], statuses_url)
    version_pages = _extend_pages(
        _as_dict(by_label["versions"], version_url(0)),
        version_url,
        email,
        api_token,
        exchange,
        workers,
    )
    versions = _items_from_pages(version_pages, "values", version_url)
    prop_names = _property_names(by_label["properties"], properties_url)
    properties = map_ordered(
        lambda name: _as_dict(
            fetch_json(property_url(properties_url, name), email, api_token, exchange),
            property_url(properties_url, name),
        ),
        prop_names,
        workers,
    )
    issues: list[dict] = []
    for key in keys:
        url = issue_url(origin, key)
        issues.append(_as_dict(by_label[url], url))
    return project_body, components, versions, statuses, properties, issues


def _load_children(
    origin: str,
    issues: list[dict],
    email: str,
    api_token: str,
    exchange,
    workers: int,
) -> dict[str, _Bundle]:
    """Comments, history, worklogs, and the other per-issue JSON.

    First pages for every issue run together. Later pages and property
    values run together after that. Results are put back in startAt order.
    """
    bundles = {issue["key"]: _Bundle() for issue in issues}
    for issue in issues:
        bundles[issue["key"]].links = _issue_links(issue)

    jobs: list[tuple] = []
    for issue in issues:
        key = issue["key"]
        if _bucket_total(issue, "comment") != 0:
            jobs.append(("comments", key, 0, paged_url(origin, f"/rest/api/3/issue/{key}/comment", 0)))
        jobs.append(("changelog", key, 0, paged_url(origin, f"/rest/api/3/issue/{key}/changelog", 0)))
        if _bucket_total(issue, "worklog") != 0:
            jobs.append(("worklog", key, 0, paged_url(origin, f"/rest/api/3/issue/{key}/worklog", 0)))
        jobs.append(("remotelink", key, 0, f"{origin}/rest/api/3/issue/{key}/remotelink"))
        jobs.append(("watchers", key, 0, f"{origin}/rest/api/3/issue/{key}/watchers"))
        jobs.append(("votes", key, 0, f"{origin}/rest/api/3/issue/{key}/votes"))
        jobs.append(("properties", key, 0, f"{origin}/rest/api/3/issue/{key}/properties"))
        for att_id in _attachment_ids(issue):
            jobs.append(("attachment", key, att_id, f"{origin}/rest/api/3/attachment/{att_id}"))

    optional = {"watchers", "votes", "changelog"}

    def _fetch_job(job: tuple):
        kind, key, _marker, url = job
        try:
            return fetch_json(url, email, api_token, exchange, optional=kind in optional)
        except PullError as exc:
            if kind != "changelog":
                raise
            print(f"skipped changelog for {key}: {exc}", file=sys.stderr)
            return None

    fetched = map_ordered(_fetch_job, jobs, workers)

    comment_pages: dict[str, dict[int, dict]] = {}
    changelog_pages: dict[str, dict[int, dict]] = {}
    worklog_pages: dict[str, dict[int, dict]] = {}
    more_pages: list[tuple] = []
    property_jobs: list[tuple] = []

    def _queue_rest(kind: str, key: str, page: dict, path: str, sink: dict[str, dict[int, dict]]) -> None:
        sink.setdefault(key, {})[0] = page
        for start in remaining_starts(page, paged_url(origin, path, 0)):
            more_pages.append((kind, key, start, paged_url(origin, path, start)))

    for job, payload in zip(jobs, fetched):
        kind, key, _marker, url = job
        bundle = bundles[key]
        if kind == "comments":
            _queue_rest(kind, key, _as_dict(payload, url), f"/rest/api/3/issue/{key}/comment", comment_pages)
        elif kind == "changelog":
            if payload is None:
                continue
            if not isinstance(payload, dict) or not changelog_ready(payload):
                print(
                    f"skipped changelog for {key}: response has no values or histories list {url}",
                    file=sys.stderr,
                )
                continue
            try:
                _queue_rest(kind, key, payload, f"/rest/api/3/issue/{key}/changelog", changelog_pages)
            except PullError as exc:
                print(f"skipped further changelog pages for {key}: {exc}", file=sys.stderr)
        elif kind == "worklog":
            _queue_rest(kind, key, _as_dict(payload, url), f"/rest/api/3/issue/{key}/worklog", worklog_pages)
        elif kind == "remotelink":
            bundle.remote_links = _as_dicts(payload, url)
        elif kind == "watchers":
            bundle.watchers = None if payload is None else _as_dict(payload, url)
        elif kind == "votes":
            bundle.votes = None if payload is None else _as_dict(payload, url)
        elif kind == "properties":
            for name in _property_names(payload, url):
                property_jobs.append((key, property_url(url, name)))
        elif kind == "attachment":
            bundle.attachments.append(_as_dict(payload, url))
        else:
            raise PullError(f"unknown pull job {kind}")

    follow_ups = [("page", item) for item in more_pages] + [("prop", item) for item in property_jobs]

    def _fetch_follow(item: tuple):
        if item[0] == "page":
            kind, key, _start, url = item[1]
            try:
                return fetch_json(url, email, api_token, exchange, optional=kind == "changelog")
            except PullError as exc:
                if kind != "changelog":
                    raise
                print(f"skipped changelog for {key}: {exc}", file=sys.stderr)
                return None
        return fetch_json(item[1][1], email, api_token, exchange)

    if follow_ups:
        follow_bodies = map_ordered(_fetch_follow, follow_ups, workers)
        for item, payload in zip(follow_ups, follow_bodies):
            if item[0] == "page":
                kind, key, start, url = item[1]
                if kind == "changelog" and payload is None:
                    continue
                if kind == "changelog" and (not isinstance(payload, dict) or not changelog_ready(payload)):
                    print(
                        f"skipped changelog for {key}: response has no values or histories list {url}",
                        file=sys.stderr,
                    )
                    continue
                page = _as_dict(payload, url)
                sink = {"comments": comment_pages, "changelog": changelog_pages, "worklog": worklog_pages}[kind]
                sink[key][start] = page
            else:
                key, url = item[1]
                bundles[key].properties.append(_as_dict(payload, url))

    for key, pages in comment_pages.items():
        ordered = [pages[start] for start in sorted(pages)]
        build = lambda start, issue_key=key: paged_url(origin, f"/rest/api/3/issue/{issue_key}/comment", start)
        bundles[key].comments = _items_from_pages(ordered, "comments", build)
    for key, pages in changelog_pages.items():
        bundles[key].changelog = [pages[start] for start in sorted(pages)]
    for key, pages in worklog_pages.items():
        ordered = [pages[start] for start in sorted(pages)]
        build = lambda start, issue_key=key: paged_url(origin, f"/rest/api/3/issue/{issue_key}/worklog", start)
        bundles[key].worklogs = _items_from_pages(ordered, "worklogs", build)
    return bundles


def collect(
    origin: str,
    project: str,
    email: str,
    api_token: str,
    exchange,
    *,
    workers: int = DEFAULT_CONCURRENCY,
) -> list[dict]:
    """Raw JSON objects in sidecar order. Nothing here is cleaned or dropped
    except attachment bytes, watchers/votes Jira refused, and a changelog
    page for one issue that was forbidden or had no values/histories list.
    """
    keys = list(iter_issue_keys(origin, project, email, api_token, exchange))
    project_body, components, versions, statuses, properties, issues = _load_roots(
        origin, project, keys, email, api_token, exchange, workers
    )
    for issue, key in zip(issues, keys):
        if issue.get("key") != key:
            raise PullError(f"GET issue {key} returned {issue.get('key')!r}")
        if not isinstance(issue.get("fields"), dict):
            raise PullError(f"GET issue {key} had no fields object")
    bundles = _load_children(origin, issues, email, api_token, exchange, workers)
    bodies: list[dict] = [project_body]
    bodies.extend(components)
    bodies.extend(versions)
    bodies.extend(statuses)
    bodies.extend(properties)
    for issue in issues:
        bundle = bundles[issue["key"]]
        bodies.append(issue)
        bodies.extend(bundle.links)
        bodies.extend(bundle.comments)
        bodies.extend(bundle.changelog)
        bodies.extend(bundle.worklogs)
        bodies.extend(bundle.attachments)
        bodies.extend(bundle.remote_links)
        if bundle.watchers is not None:
            bodies.append(bundle.watchers)
        if bundle.votes is not None:
            bodies.append(bundle.votes)
        bodies.extend(bundle.properties)
    return bodies


def iter_envelopes(
    origin: str,
    project: str,
    email: str,
    api_token: str,
    exchange,
    *,
    workers: int = DEFAULT_CONCURRENCY,
) -> Iterator[dict]:
    """One {type, body} envelope per collected object, in collect() order."""
    for payload in collect(origin, project, email, api_token, exchange, workers=workers):
        yield wrap_jira(payload)


def emit_jsonl(envelopes: Iterator[dict], stream) -> int:
    count = 0
    for envelope in envelopes:
        stream.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        count += 1
    stream.flush()
    return count


def readable_sidecar(output: Path) -> Path:
    """Glance file written next to an --output JSONL path."""
    return Path(str(output) + ".readable.md")


SUMMARY_SECTIONS = (
    ("project", "Project"),
    ("component", "Components"),
    ("version", "Versions"),
    ("statuses", "Issue type statuses"),
    ("project_property", "Project properties"),
    ("issue", "Issues"),
    ("issuelink", "Issue links"),
    ("comment", "Comments"),
    ("changelog", "Changelog pages"),
    ("worklog", "Worklogs"),
    ("attachment", "Attachments"),
    ("remote_link", "Remote links"),
    ("watchers", "Watchers"),
    ("votes", "Votes"),
    ("issue_property", "Issue properties"),
    ("other", "Other"),
)


def _self_url(body: dict) -> str:
    self_url = body.get("self")
    return self_url if isinstance(self_url, str) else ""


def classify(body: dict) -> str:
    """Sidecar label for a raw object. The JSONL body is not changed."""
    self_url = _self_url(body)
    if "/comment/" in self_url:
        return "comment"
    if "/worklog/" in self_url:
        return "worklog"
    if "/attachment/" in self_url:
        return "attachment"
    if "/changelog" in self_url or (
        isinstance(body.get("histories"), list) and "startAt" in body and "total" in body
    ) or (
        isinstance(body.get("values"), list)
        and "startAt" in body
        and "total" in body
        and (
            not body["values"]
            or (isinstance(body["values"][0], dict) and "items" in body["values"][0])
        )
    ):
        return "changelog"
    if "/issueLink/" in self_url or "outwardIssue" in body or "inwardIssue" in body:
        return "issuelink"
    if self_url.rstrip("/").endswith("/watchers") or (
        isinstance(body.get("watchers"), list) and "watchCount" in body
    ):
        return "watchers"
    if self_url.rstrip("/").endswith("/votes") or ("hasVoted" in body and "votes" in body):
        return "votes"
    if "/remotelink/" in self_url or (
        isinstance(body.get("object"), dict) and ("globalId" in body or "relationship" in body)
    ):
        return "remote_link"
    fields = body.get("fields")
    key = body.get("key")
    if isinstance(fields, dict) and isinstance(key, str) and ISSUE_KEY.fullmatch(key):
        return "issue"
    if "/component/" in self_url or "assigneeType" in body:
        return "component"
    if "/version/" in self_url or ("released" in body and "name" in body and "fields" not in body):
        return "version"
    if "/project/" in self_url and "/properties/" in self_url:
        return "project_property"
    if "/properties/" in self_url or (
        isinstance(key, str) and "value" in body and "fields" not in body and "histories" not in body
    ):
        return "issue_property" if "/issue/" in self_url or "value" in body else "project_property"
    if isinstance(body.get("statuses"), list) and isinstance(body.get("name"), str):
        return "statuses"
    if isinstance(key, str) and PROJECT_KEY.fullmatch(key) and "fields" not in body:
        return "project"
    if "filename" in body and "mimeType" in body and "size" in body:
        return "attachment"
    if "timeSpentSeconds" in body or ("timeSpent" in body and "started" in body):
        return "worklog"
    if "updateAuthor" in body and "created" in body and "body" in body and "fields" not in body:
        return "comment"
    return "other"


GLANCE_LIMIT = 120
_ADF_BLOCKS = {
    "paragraph",
    "heading",
    "blockquote",
    "codeBlock",
    "listItem",
    "bulletList",
    "orderedList",
    "panel",
    "tableCell",
    "tableHeader",
    "tableRow",
    "rule",
}


def _adf_text(node, depth: int = 0) -> str:
    """Flatten an Atlassian document to plain text. Presentation only."""
    if depth > 32:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(item, depth + 1) for item in node)
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return node.get("text") if isinstance(node.get("text"), str) else ""
    if node_type == "hardBreak":
        return " "
    attrs = node.get("attrs")
    own = ""
    if isinstance(attrs, dict) and isinstance(attrs.get("text"), str):
        own = attrs["text"]
    content = node.get("content")
    inner = _adf_text(content, depth + 1) if isinstance(content, list) else ""
    if node_type in _ADF_BLOCKS:
        return f"{own}{inner} "
    return f"{own}{inner}"


def plain_text(value) -> str:
    """String, ADF document, or empty. Does not rewrite the stored value."""
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (dict, list)):
        raw = _adf_text(value)
    else:
        return ""
    return " ".join(raw.split())


def excerpt(value, limit: int = GLANCE_LIMIT) -> str:
    flat = plain_text(value)
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _person_name(body: dict) -> str:
    for field in ("author", "updateAuthor"):
        person = body.get(field)
        if isinstance(person, dict):
            name = person.get("displayName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def _join_glance(*parts: str) -> str:
    kept = [part for part in parts if part]
    if not kept:
        return "?"
    line = kept[0]
    for part in kept[1:]:
        line = f"{line} — {part}"
    return line


def _issue_glance(body: dict) -> str:
    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    status = ""
    status_obj = fields.get("status")
    if isinstance(status_obj, dict) and isinstance(status_obj.get("name"), str):
        status = status_obj["name"].strip()
    key = str(body.get("key") or "?")
    head = f"{key} {status}".rstrip()
    return _join_glance(head, excerpt(fields.get("summary")), excerpt(fields.get("description")))


def _comment_glance(body: dict) -> str:
    who = " ".join(part for part in (str(body.get("id") or ""), _person_name(body)) if part)
    return _join_glance(who or "?", excerpt(body.get("body")))


def _worklog_glance(body: dict) -> str:
    spent = body.get("timeSpent") if isinstance(body.get("timeSpent"), str) else ""
    who = " ".join(part for part in (str(body.get("id") or ""), _person_name(body), spent) if part)
    return _join_glance(who or "?", excerpt(body.get("comment")))


def _changelog_glance(body: dict) -> str:
    entries = body.get("values") if isinstance(body.get("values"), list) else None
    if entries is None and isinstance(body.get("histories"), list):
        entries = body["histories"]
    bits: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
            continue
        for item in entry["items"]:
            if not isinstance(item, dict):
                continue
            field = item.get("field") if isinstance(item.get("field"), str) else "field"
            before = item.get("fromString") if isinstance(item.get("fromString"), str) else ""
            after = item.get("toString") if isinstance(item.get("toString"), str) else ""
            if before and after:
                bits.append(f"{field}: {before} → {after}")
            elif after or before:
                bits.append(f"{field}: {after or before}")
            else:
                bits.append(field)
    return _join_glance(f"startAt {body.get('startAt')}", excerpt("; ".join(bits)))


def _issuelink_glance(body: dict) -> str:
    link_type = body.get("type")
    kind = ""
    if isinstance(link_type, dict) and isinstance(link_type.get("name"), str):
        kind = link_type["name"]
    target = ""
    for field, arrow in (("outwardIssue", "→"), ("inwardIssue", "←")):
        issue = body.get(field)
        if isinstance(issue, dict) and isinstance(issue.get("key"), str):
            target = f"{arrow} {issue['key']}"
            break
    ident = str(body.get("id") or "")
    return " ".join(part for part in (ident, kind, target) if part) or "?"


def _names(people) -> str:
    if not isinstance(people, list):
        return ""
    names = [
        person["displayName"].strip()
        for person in people
        if isinstance(person, dict)
        and isinstance(person.get("displayName"), str)
        and person["displayName"].strip()
    ]
    return excerpt(", ".join(names))


def _property_glance(body: dict) -> str:
    key = str(body.get("key") or "?")
    value = body.get("value")
    if isinstance(value, str):
        shown = excerpt(value)
    elif value is None:
        shown = ""
    else:
        shown = excerpt(json.dumps(value, separators=(",", ":"), default=str))
    return _join_glance(key, shown)


def _summary_label(kind: str, body: dict) -> str:
    if kind == "issue":
        return _issue_glance(body)
    if kind == "comment":
        return _comment_glance(body)
    if kind == "worklog":
        return _worklog_glance(body)
    if kind == "changelog":
        return _changelog_glance(body)
    if kind == "issuelink":
        return _issuelink_glance(body)
    if kind == "attachment":
        filename = str(body.get("filename") or body.get("id") or "?")
        mime = body.get("mimeType") if isinstance(body.get("mimeType"), str) else ""
        return f"{filename} ({mime})" if mime else filename
    if kind == "remote_link":
        obj = body.get("object") if isinstance(body.get("object"), dict) else {}
        title = obj.get("title") if isinstance(obj.get("title"), str) else ""
        ident = str(body.get("id") or "")
        if title:
            return _join_glance(ident, title)
        return ident or "?"
    if kind == "watchers":
        return _join_glance(f"watchCount {body.get('watchCount')}", _names(body.get("watchers")))
    if kind == "votes":
        return _join_glance(f"votes {body.get('votes')}", _names(body.get("voters")))
    if kind in ("project_property", "issue_property"):
        return _property_glance(body)
    if kind in ("component", "version", "statuses", "project"):
        return str(body.get("name") or body.get("key") or body.get("id") or "?")
    self_url = _self_url(body)
    return self_url or str(body.get("id") or body.get("key") or "?")


def render_summary(project: str, envelopes: list[dict]) -> str:
    """Glance lines for a human. Does not replace or rewrite the JSONL intake."""
    groups: dict[str, list[str]] = {kind: [] for kind, _heading in SUMMARY_SECTIONS}
    for envelope in envelopes:
        body = envelope.get("body")
        if not isinstance(body, dict):
            continue
        kind = classify(body)
        groups.setdefault(kind, []).append(_summary_label(kind, body))
    parts = [f"# {project}", ""]
    for kind, heading in SUMMARY_SECTIONS:
        labels = groups.get(kind, [])
        parts.append(f"## {heading} ({len(labels)})")
        parts.append("")
        parts.append("\n".join(f"- {label}" for label in labels) if labels else "- (none)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def bind_exchange(
    exchange,
    gate: RateLimitGate,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    jitter: Jitter | None = None,
):
    """Wrap a raw exchange so every pull request shares one rate-limit gate."""

    def bound(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        return exchange_with_retry(
            exchange,
            method,
            url,
            headers,
            body,
            sleep=sleep,
            clock=clock,
            jitter=jitter,
            gate=gate,
        )

    return bound


def pull(
    base_url: str,
    email: str,
    api_token: str,
    project: str,
    exchange,
    *,
    workers: int = DEFAULT_CONCURRENCY,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    jitter: Jitter | None = None,
    gate: RateLimitGate | None = None,
) -> Iterator[dict]:
    if not email or not api_token:
        raise PullError("Set JIRA_EMAIL and JIRA_API_TOKEN.")
    origin = site_origin(base_url)
    if not PROJECT_KEY.fullmatch(project):
        raise PullError(
            "project must be a Jira project key such as EMBER "
            "(uppercase letters and digits, 2-10 characters)."
        )
    gate = gate or RateLimitGate()
    bound = bind_exchange(exchange, gate, sleep=sleep, clock=clock, jitter=jitter)
    return iter_envelopes(origin, project, email, api_token, bound, workers=workers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pull one Jira project into EMBER-39 JSONL. "
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
    parser.add_argument(
        "--no-readable",
        action="store_true",
        help="Skip the glance summary. JSONL only.",
    )
    parser.add_argument(
        "--project",
        help=f"Project key. Defaults to JIRA_TEST_PROJECT, then {DEFAULT_PROJECT}.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Parallel detail GETs (default {DEFAULT_CONCURRENCY}, "
        f"max {MAX_CONCURRENCY}, or JIRA_PULL_CONCURRENCY). Search pages stay serial.",
    )
    args = parser.parse_args(argv)
    if args.no_readable and args.readable:
        print("Use only one of --readable or --no-readable.", file=sys.stderr)
        return 2
    if args.readable == "-" and not args.output:
        print("--readable - needs --output so JSONL is not mixed into stdout.", file=sys.stderr)
        return 2

    base_url = os.environ.get("JIRA_BASE_URL", "").strip()
    email = os.environ.get("JIRA_EMAIL", "").strip()
    api_token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not base_url:
        print("Set JIRA_BASE_URL to the site origin, like https://<site>.", file=sys.stderr)
        return 2
    if not email or not api_token:
        print("Set JIRA_EMAIL and JIRA_API_TOKEN.", file=sys.stderr)
        return 2

    try:
        project = resolve_project(args.project)
        workers = resolve_concurrency(args.concurrency)
        envelopes = list(pull(base_url, email, api_token, project, urllib_exchange, workers=workers))
        if args.output:
            output = Path(args.output)
            with output.open("w", encoding="utf-8") as handle:
                count = emit_jsonl(envelopes, handle)
            print(f"wrote {count} envelopes to {output}", file=sys.stderr)
        else:
            count = emit_jsonl(envelopes, sys.stdout)
            print(f"wrote {count} envelopes", file=sys.stderr)
        if not args.no_readable:
            summary = render_summary(project, envelopes)
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
