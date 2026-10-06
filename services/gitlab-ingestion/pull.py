"""GitLab backfill and reconciliation for one project.

Reads a project through REST API v4 with an access token (read_api) and
writes one EMBER-39 envelope per discrete REST object:
  {"type":"gitlab","body":<raw GitLab REST object>}
Stdout is that JSONL. Progress goes to stderr.

Backfill (default) pulls, in JSONL order: the project, every merge request,
each merge request's notes, every issue, and each issue's notes. List
objects are emitted as GitLab returns them; they already carry the
description, state, author, assignees, reviewers and labels.

Reconciliation (--updated-after or --lookback-hours) pulls the project plus
only the merge requests and issues updated after the cutoff, with their
notes. A new note bumps its parent's updated_at, so new comments are
covered. Run it on a schedule to re-cover anything a disabled webhook
dropped. Overlapping windows are fine: the pipeline upserts on stable ids.

Confidential issues and internal notes are skipped, matching the webhook
triggers that stay off for the first cut (docs/ingestion/gitlab.md). The
summary counts what was skipped.

Rate limits: every GitLab response carries RateLimit-Remaining and
RateLimit-Reset. When the remaining quota runs low, every worker pauses
until the reset. A 429 waits out Retry-After (or RateLimit-Reset). A
429/502/503/504 without either, or a network error, backs off 1s, 2s, 4s,
... Waits are capped at MAX_RETRY_WAIT. One gate is shared by all workers.

--output FILE writes the JSONL there and, by default, a glance summary at
FILE.readable.md. --readable PATH chooses the summary file. --no-readable
skips it. --readable - prints the summary on stdout and requires --output
so the JSONL stays a file.

The webhook receiver does not use this script and never holds the API token.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from envelope import wrap_gitlab

USER_AGENT = "ember-gitlab-ingestion"
DEFAULT_BASE_URL = "https://gitlab.com"
API_PREFIX = "/api/v4"
PAGE_SIZE = 100
MAX_PAGES = 200
DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 32
MAX_RETRY_WAIT = 120.0
# Pause every worker when this few requests remain in the window. Above
# MAX_CONCURRENCY so requests already in flight cannot spend the rest.
RATE_LIMIT_FLOOR = 50
GLANCE_LIMIT = 80
PROJECT_PATH = re.compile(r"^[\w.-]+(/[\w.-]+)+$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class PullError(Exception):
    pass


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


def resolve_base_url(raw: str | None) -> str:
    """https URL of the instance, with an optional relative root. Defaults to gitlab.com.

    http is accepted only for a local instance, so the token never crosses
    the network in clear text.
    """
    text = (raw or "").strip().rstrip("/") or DEFAULT_BASE_URL
    parts = urlsplit(text)
    local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
    if parts.scheme != "https" and not local:
        raise PullError("GITLAB_BASE_URL must be https://<host>, like https://gitlab.com")
    if not parts.hostname or "@" in parts.netloc or parts.query or parts.fragment:
        raise PullError("GITLAB_BASE_URL must be a plain URL with no credentials, query, or fragment")
    if parts.path.endswith(API_PREFIX):
        raise PullError(f"GITLAB_BASE_URL is the instance URL, without {API_PREFIX}")
    return text


def resolve_project(cli_value: str | None) -> str:
    """CLI wins, then GITLAB_PROJECT. A numeric id or a full path like group/project."""
    raw = cli_value if cli_value is not None else os.environ.get("GITLAB_PROJECT", "")
    project = raw.strip().strip("/")
    if not project:
        raise PullError("Set GITLAB_PROJECT (or --project) to a project id or full path, like group/project")
    if not (project.isdigit() or PROJECT_PATH.fullmatch(project)):
        raise PullError("GITLAB_PROJECT must be a numeric id or a full path such as group/project")
    return project


def resolve_concurrency(cli_value: int | None) -> int:
    """CLI wins, then GITLAB_PULL_CONCURRENCY, then DEFAULT_CONCURRENCY."""
    if cli_value is None:
        raw = os.environ.get("GITLAB_PULL_CONCURRENCY", "").strip()
        if not raw:
            return DEFAULT_CONCURRENCY
        try:
            cli_value = int(raw)
        except ValueError as exc:
            raise PullError("GITLAB_PULL_CONCURRENCY must be an integer") from exc
    if cli_value < 1 or cli_value > MAX_CONCURRENCY:
        raise PullError(f"concurrency must be from 1 to {MAX_CONCURRENCY}")
    return cli_value


def resolve_cutoff(
    updated_after: str | None,
    lookback_hours: float | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """ISO 8601 UTC cutoff for reconciliation, or None for a full backfill."""
    if updated_after is not None and lookback_hours is not None:
        raise PullError("use only one of --updated-after or --lookback-hours")
    if lookback_hours is not None:
        if lookback_hours <= 0:
            raise PullError("--lookback-hours must be greater than 0")
        moment = (now or datetime.now(timezone.utc)) - timedelta(hours=lookback_hours)
    elif updated_after is not None:
        try:
            moment = datetime.fromisoformat(updated_after.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise PullError("--updated-after must be ISO 8601, like 2026-10-01T00:00:00Z") from exc
        if moment.tzinfo is None:
            raise PullError("--updated-after needs a timezone, like 2026-10-01T00:00:00Z")
    else:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def _header_float(response: Response, name: str) -> float | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def retry_after_seconds(response: Response, attempt: int = 0, *, now: float | None = None) -> float | None:
    """Seconds to wait before retrying, or None when the response is final.

    Retry-After wins. A 429 without it waits until RateLimit-Reset. A
    429/502/503/504 with neither waits 2**attempt seconds (1, 2, 4, ...).
    """
    if response.status < 400:
        return None
    retry_after = _header_float(response, "retry-after")
    if retry_after is not None:
        return max(0.0, retry_after)
    if response.status == 429:
        reset = _header_float(response, "ratelimit-reset")
        if reset is not None:
            clock = time.time() if now is None else now
            return max(0.0, reset - clock)
    if response.status in (429, 502, 503, 504):
        return float(2 ** max(0, attempt))
    return None


def throttle_seconds(response: Response, *, now: float | None = None) -> float | None:
    """Seconds to pause before the next request because the quota is nearly spent.

    Reads RateLimit-Remaining and RateLimit-Reset, which GitLab sends on every
    response. The floor shrinks for small limits so a low-limit instance does
    not pause on every request.
    """
    remaining = _header_float(response, "ratelimit-remaining")
    reset = _header_float(response, "ratelimit-reset")
    if remaining is None or reset is None:
        return None
    limit = _header_float(response, "ratelimit-limit")
    floor = RATE_LIMIT_FLOOR if limit is None else min(RATE_LIMIT_FLOOR, max(1, int(limit) // 10))
    if remaining > floor:
        return None
    clock = time.time() if now is None else now
    wait = reset - clock
    return wait if wait > 0 else None


def exchange_with_retry(
    exchange,
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    gate: RateLimitGate | None = None,
    clock: Callable[[], float] = time.time,
) -> Response:
    """Send one request, pausing for a low quota and retrying throttled responses.

    ``gate`` is shared by every worker in one pull. A throttle extends it so
    the next request from any worker waits out the same window.
    """
    response: Response | None = None
    for attempt in range(attempts):
        if gate is not None:
            gate.wait()
        response = exchange(method, url, headers)
        pause = throttle_seconds(response, now=clock())
        if pause is not None and gate is not None:
            pause = min(pause, MAX_RETRY_WAIT)
            gate.extend(pause)
            print(f"GitLab quota low; pausing {pause:.0f}s", file=sys.stderr)
        wait = retry_after_seconds(response, attempt, now=clock())
        if wait is None or attempt == attempts - 1:
            return response
        wait = min(wait, MAX_RETRY_WAIT)
        if gate is not None:
            gate.extend(wait)
        print(f"GitLab asked to wait {wait:.0f}s ({response.status}) {url}", file=sys.stderr)
        sleep(wait)
    return response


def map_ordered(fn, items: list, workers: int) -> list:
    """Run fn over items concurrently and return results in input order."""
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def urllib_exchange(method: str, url: str, headers: dict[str, str]) -> Response:
    """One HTTP exchange. A network failure comes back as a 503 so it is retried
    with the same backoff instead of ending a long backfill."""
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        return Response(503, {}, f"network error: {reason}".encode("utf-8"))


def _headers(token: str) -> dict[str, str]:
    return {"PRIVATE-TOKEN": token, "Accept": "application/json", "User-Agent": USER_AGENT}


def _read_json(response: Response, what: str):
    if response.status in (401, 403):
        raise PullError(
            f"{what} failed ({response.status}). Check that GITLAB_API_TOKEN is valid, "
            "has the read_api scope, and can see this project."
        )
    if response.status == 404:
        raise PullError(f"{what} failed (404). Check GITLAB_PROJECT and that the token can see it.")
    if response.status >= 400:
        detail = response.body.decode("utf-8", "replace")[:500]
        raise PullError(f"{what} failed ({response.status}): {detail}")
    return response.json()


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        start = part.find("<")
        end = part.find(">")
        if start != -1 and end != -1:
            return part[start + 1 : end]
    return None


class Capture:
    """Everything one pull fetched, in JSONL order, plus what it skipped."""

    def __init__(self, records: list[tuple[str, dict]], skipped: dict[str, int]) -> None:
        self.records = records
        self.skipped = skipped

    def envelopes(self) -> list[dict]:
        return [wrap_gitlab(body) for _, body in self.records]


def _iids(items: list[dict], label: str) -> list[int]:
    found = []
    for item in items:
        iid = item.get("iid")
        if not isinstance(iid, int) or isinstance(iid, bool):
            raise PullError(f"{label} without an iid")
        found.append(iid)
    return found


def _is_internal(note: dict) -> bool:
    return note.get("internal") is True or note.get("confidential") is True


def _kept_notes(kind: str, groups: list[list[dict]], skipped: dict[str, int]) -> list[tuple[str, dict]]:
    """Flatten per-parent note lists, dropping internal notes and counting them."""
    kept = []
    for group in groups:
        for note in group:
            if _is_internal(note):
                skipped["internal_notes"] += 1
                continue
            kept.append((kind, note))
    return kept


class GitlabPull:
    """REST pull for one project. HTTP stays inside ``workers`` at a time."""

    def __init__(
        self,
        base_url: str,
        project: str,
        token: str,
        exchange,
        workers: int,
        gate: RateLimitGate | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api = base_url + API_PREFIX
        self.project = project
        self.token = token
        self.exchange = exchange
        self.workers = workers
        self.gate = gate or RateLimitGate()
        self.sleep = sleep
        self.clock = clock

    def _request(self, url: str) -> Response:
        return exchange_with_retry(
            self.exchange,
            "GET",
            url,
            _headers(self.token),
            sleep=self.sleep,
            gate=self.gate,
            clock=self.clock,
        )

    def get_object(self, url: str) -> dict:
        payload = _read_json(self._request(url), f"GET {url}")
        if not isinstance(payload, dict):
            raise PullError(f"expected a JSON object from {url}")
        return payload

    def get_list(self, url: str) -> list[dict]:
        """Follow rel="next" links. A link off this instance's API is refused, so
        the token is never sent anywhere else."""
        found: list[dict] = []
        pages = 0
        while url:
            pages += 1
            if pages > MAX_PAGES:
                raise PullError(f"stopped after {MAX_PAGES} pages ({MAX_PAGES * PAGE_SIZE} objects) for {url}")
            response = self._request(url)
            payload = _read_json(response, f"GET {url}")
            if not isinstance(payload, list):
                raise PullError(f"expected a JSON list from {url}")
            for item in payload:
                if not isinstance(item, dict):
                    raise PullError(f"expected objects in {url}")
                found.append(item)
            next_url = parse_next_link(response.headers.get("link"))
            if next_url is not None and not next_url.startswith(self.api + "/"):
                raise PullError(f"refusing pagination link outside {self.api}: {next_url}")
            url = next_url
        return found

    def _list_url(self, path: str, params: dict[str, str]) -> str:
        query = {**params, "per_page": str(PAGE_SIZE)}
        return f"{self.api}{path}?{urlencode(query)}"

    def capture(self, cutoff: str | None = None, progress: Callable[[str], None] | None = None) -> Capture:
        log = progress or (lambda _message: None)
        project = self.get_object(f"{self.api}/projects/{quote(self.project, safe='')}")
        project_id = project.get("id")
        if not isinstance(project_id, int) or isinstance(project_id, bool):
            raise PullError(f"project {self.project} has no numeric id")
        base = f"/projects/{project_id}"
        # created_at, not updated_at: offset pages stay stable when an item is
        # edited mid-pull, so nothing slides between pages and gets skipped.
        window = {"updated_after": cutoff} if cutoff else {}
        log(f"project {project.get('path_with_namespace', self.project)} (id {project_id})")

        merge_requests = self.get_list(
            self._list_url(f"{base}/merge_requests", {"state": "all", "order_by": "created_at", "sort": "asc", **window})
        )
        log(f"{len(merge_requests)} merge requests")
        all_issues = self.get_list(
            self._list_url(
                f"{base}/issues",
                {"state": "all", "scope": "all", "order_by": "created_at", "sort": "asc", **window},
            )
        )
        issues = [issue for issue in all_issues if issue.get("confidential") is not True]
        skipped = {"confidential_issues": len(all_issues) - len(issues), "internal_notes": 0}
        log(f"{len(issues)} issues ({skipped['confidential_issues']} confidential skipped)")

        def notes_for(kind: str) -> Callable[[int], list[dict]]:
            def fetch(iid: int) -> list[dict]:
                return self.get_list(
                    self._list_url(f"{base}/{kind}/{iid}/notes", {"sort": "asc", "order_by": "created_at"})
                )

            return fetch

        mr_notes = map_ordered(notes_for("merge_requests"), _iids(merge_requests, "merge request"), self.workers)
        issue_notes = map_ordered(notes_for("issues"), _iids(issues, "issue"), self.workers)

        records: list[tuple[str, dict]] = [("project", project)]
        records += [("merge_request", item) for item in merge_requests]
        records += _kept_notes("merge_request_note", mr_notes, skipped)
        records += [("issue", item) for item in issues]
        records += _kept_notes("issue_note", issue_notes, skipped)
        notes = sum(1 for kind, _ in records if kind.endswith("_note"))
        log(f"{notes} notes ({skipped['internal_notes']} internal skipped)")
        return Capture(records, skipped)


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


def excerpt(value, limit: int = GLANCE_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _username(body: dict) -> str:
    author = body.get("author")
    if isinstance(author, dict) and isinstance(author.get("username"), str):
        return "@" + author["username"]
    return "(unknown)"


def _join(*parts: str) -> str:
    return " — ".join(part for part in parts if part)


def _summary_line(kind: str, body: dict) -> str:
    if kind == "project":
        return _join(str(body.get("path_with_namespace", "")), excerpt(body.get("description")))
    if kind == "merge_request":
        return _join(f"!{body.get('iid')} {excerpt(body.get('title'))}", str(body.get("state", "")), excerpt(body.get("description")))
    if kind == "issue":
        return _join(f"#{body.get('iid')} {excerpt(body.get('title'))}", str(body.get("state", "")), excerpt(body.get("description")))
    marker = "!" if body.get("noteable_type") == "MergeRequest" else "#"
    system = " (system)" if body.get("system") is True else ""
    return _join(f"on {marker}{body.get('noteable_iid')}", f"{_username(body)}{system}: {excerpt(body.get('body'))}")


SECTIONS = (
    ("project", "Project"),
    ("merge_request", "Merge requests"),
    ("merge_request_note", "Merge request notes"),
    ("issue", "Issues"),
    ("issue_note", "Issue notes"),
)


def render_summary(project: str, capture: Capture, cutoff: str | None) -> str:
    mode = f"reconciliation, updated after {cutoff}" if cutoff else "full backfill"
    lines = [f"# GitLab intake: {project}", "", f"Mode: {mode}. {len(capture.records)} envelopes.", ""]
    for kind, title in SECTIONS:
        lines.append(f"- {title}: {sum(1 for found, _ in capture.records if found == kind)}")
    lines.append(f"- Skipped confidential issues: {capture.skipped['confidential_issues']}")
    lines.append(f"- Skipped internal notes: {capture.skipped['internal_notes']}")
    for kind, title in SECTIONS:
        bodies = [body for found, body in capture.records if found == kind]
        if not bodies:
            continue
        lines += ["", f"## {title}", ""]
        lines += [f"- {_summary_line(kind, body)}" for body in bodies]
    return "\n".join(lines) + "\n"


def pull(
    base_url: str,
    project: str,
    token: str,
    exchange,
    *,
    workers: int = DEFAULT_CONCURRENCY,
    cutoff: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> Capture:
    return GitlabPull(base_url, project, token, exchange, workers).capture(cutoff, progress)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pull one GitLab project into EMBER-39 JSONL: a full backfill, or a reconciliation "
            "with --updated-after / --lookback-hours. "
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
    parser.add_argument("--project", help="Project id or full path. Defaults to GITLAB_PROJECT.")
    parser.add_argument(
        "--updated-after",
        metavar="TIMESTAMP",
        help="Reconcile: only merge requests and issues updated after this ISO 8601 time.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=None,
        help="Reconcile: only merge requests and issues updated in the last N hours.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Parallel note fetches (default {DEFAULT_CONCURRENCY}, max {MAX_CONCURRENCY}, "
        "or GITLAB_PULL_CONCURRENCY). List pages stay serial.",
    )
    args = parser.parse_args(argv)
    if args.no_readable and args.readable:
        print("Use only one of --readable or --no-readable.", file=sys.stderr)
        return 2
    if args.readable == "-" and not args.output:
        print("--readable - needs --output so JSONL is not mixed into stdout.", file=sys.stderr)
        return 2

    token = os.environ.get("GITLAB_API_TOKEN", "").strip()
    if not token:
        print("Set GITLAB_API_TOKEN to an access token with the read_api scope.", file=sys.stderr)
        return 2

    try:
        base_url = resolve_base_url(os.environ.get("GITLAB_BASE_URL"))
        project = resolve_project(args.project)
        workers = resolve_concurrency(args.concurrency)
        cutoff = resolve_cutoff(args.updated_after, args.lookback_hours)
        capture = pull(
            base_url,
            project,
            token,
            urllib_exchange,
            workers=workers,
            cutoff=cutoff,
            progress=lambda message: print(message, file=sys.stderr),
        )
        envelopes = capture.envelopes()
        if args.output:
            output = Path(args.output)
            with output.open("w", encoding="utf-8") as handle:
                count = emit_jsonl(envelopes, handle)
            print(f"wrote {count} envelopes to {output}", file=sys.stderr)
        else:
            count = emit_jsonl(envelopes, sys.stdout)
            print(f"wrote {count} envelopes", file=sys.stderr)
        if not args.no_readable:
            summary = render_summary(project, capture, cutoff)
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
