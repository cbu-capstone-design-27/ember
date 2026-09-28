"""Local Jira pull for one test project.

Reads issues with HTTP basic auth (email + API token) and writes one
EMBER-39 envelope per issue. Pull requests do not exist in Jira.

Machine intake is JSONL, one object per line:
  {"type":"jira","body":<opaque raw Jira issue object>}
Stdout is that JSONL. --output FILE writes the same JSONL and, by default,
a glance summary at FILE.readable.md (issue key, status, summary, short
description). Search pages stay serial. Issue GETs run DEFAULT_CONCURRENCY
at a time (--concurrency or JIRA_PULL_CONCURRENCY), then the JSONL is
written in search order. A Retry-After on 429/502/503 is waited out.
--readable PATH chooses the summary file. --no-readable skips it.
--readable - prints the summary on stdout and requires --output so the
JSONL stays a file. Progress goes to stderr.

The webhook receiver does not use this script. JIRA_TEST_PROJECT is the
test target only (default EMBER). Nothing here filters live deliveries.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from envelope import wrap_jira

USER_AGENT = "ember-jira-ingestion"
MAX_PAGES = 50
PAGE_SIZE = 100
DEFAULT_CONCURRENCY = 12
MAX_CONCURRENCY = 32
MAX_RETRY_WAIT = 120.0
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


def retry_after_seconds(response: Response) -> float | None:
    """Seconds to wait on a Jira throttle, or None when the response is final."""
    if response.status < 400:
        return None
    headers = {key.lower(): value for key, value in response.headers.items()}
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    if response.status in (429, 502, 503):
        return 1.0
    return None


def exchange_with_retry(
    exchange,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    *,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> Response:
    """Repeat when Jira sends Retry-After or a 429/502/503."""
    response = exchange(method, url, headers, body)
    for _ in range(attempts - 1):
        wait = retry_after_seconds(response)
        if wait is None:
            return response
        wait = min(wait, MAX_RETRY_WAIT)
        print(f"Jira asked to wait {wait:.0f}s ({response.status}) {url}", file=sys.stderr)
        sleep(wait)
        response = exchange(method, url, headers, body)
    return response


def map_ordered(fn, items: list, workers: int) -> list:
    """Run fn over items concurrently and return results in input order."""
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def urllib_exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())


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
        response = exchange_with_retry(
            exchange,
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


def fetch_issue(origin: str, key: str, email: str, api_token: str, exchange) -> dict:
    """GET one issue. Default fields include description. The object is not edited."""
    url = f"{origin}/rest/api/3/issue/{key}"
    response = exchange_with_retry(exchange, "GET", url, _headers(email, api_token), None)
    payload = _read_json(response, f"GET {url}")
    if not isinstance(payload, dict):
        raise PullError(f"expected a JSON object from {url}")
    return payload


def iter_envelopes(
    origin: str,
    project: str,
    email: str,
    api_token: str,
    exchange,
    *,
    workers: int = DEFAULT_CONCURRENCY,
) -> Iterator[dict]:
    """Full issue GETs, yielded in search order."""
    keys = list(iter_issue_keys(origin, project, email, api_token, exchange))
    for payload in map_ordered(
        lambda key: fetch_issue(origin, key, email, api_token, exchange),
        keys,
        workers,
    ):
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


def _adf_text(node: dict) -> str:
    chunks: list[str] = []
    text = node.get("text")
    if isinstance(text, str):
        chunks.append(text)
    content = node.get("content")
    if isinstance(content, list):
        for child in content:
            if isinstance(child, dict):
                chunks.append(_adf_text(child))
            elif isinstance(child, str):
                chunks.append(child)
    return " ".join(part for part in chunks if part)


def description_text(value) -> str:
    """Plain text for the glance file. ADF stays intact on the JSONL body."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _adf_text(value)
    return ""


def _description_excerpt(value, limit: int = 80) -> str:
    flat = " ".join(description_text(value).split())
    if not flat:
        return ""
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _summary_line(body: dict) -> str | None:
    key = body.get("key")
    fields = body.get("fields")
    if not isinstance(key, str) or not isinstance(fields, dict):
        return None
    status = "unknown"
    status_obj = fields.get("status")
    if isinstance(status_obj, dict):
        name = status_obj.get("name")
        if isinstance(name, str) and name:
            status = name
    summary = fields.get("summary") if isinstance(fields.get("summary"), str) else ""
    line = f"- {key} {status} — {summary}".rstrip()
    excerpt = _description_excerpt(fields.get("description"))
    if excerpt:
        line = f"{line} — {excerpt}"
    return line


def render_summary(project: str, envelopes: list[dict]) -> str:
    """Grouped glance list. Does not replace the JSONL intake."""
    seen: set[str] = set()
    lines: list[str] = []
    for envelope in envelopes:
        body = envelope.get("body")
        if not isinstance(body, dict):
            continue
        key = body.get("key")
        line = _summary_line(body)
        if line is None or not isinstance(key, str) or key in seen:
            continue
        seen.add(key)
        lines.append(line)
    body_text = "\n".join(lines) if lines else "- (none)"
    return f"# {project}\n\n## Issues\n\n{body_text}\n"


def pull(
    base_url: str,
    email: str,
    api_token: str,
    project: str,
    exchange,
    *,
    workers: int = DEFAULT_CONCURRENCY,
) -> Iterator[dict]:
    if not email or not api_token:
        raise PullError("Set JIRA_EMAIL and JIRA_API_TOKEN.")
    origin = site_origin(base_url)
    if not PROJECT_KEY.fullmatch(project):
        raise PullError(
            "project must be a Jira project key such as EMBER "
            "(uppercase letters and digits, 2-10 characters)."
        )
    return iter_envelopes(origin, project, email, api_token, exchange, workers=workers)


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
        help=f"Parallel issue detail GETs (default {DEFAULT_CONCURRENCY}, "
        "or JIRA_PULL_CONCURRENCY). Search pages stay serial.",
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
