"""Local GitHub App pull for one test repository.

Mints an App JWT (RS256 via the openssl CLI — not a Python dependency),
exchanges it for an installation token, and writes one EMBER-39 envelope
per discrete REST object:

  {"type":"github","body":<opaque raw GitHub API object>}

Issues and pull requests are full GETs (the description is `body` on that
object, JSON null when empty). Issue comments, including conversation
comments on pull requests, and review comments are the list objects (they
already contain `body`). Reviews are listed per pull request. Commits are
listed from every branch, then from each pull request, and each SHA is
written once. Branch list objects are the tip refs. Releases, tags,
annotated tag objects, labels, milestones, issue events, commit comments,
contributors, and the repository object are included.

Stdout is JSONL. --output FILE also writes FILE.readable.md with counts
and keys, including how many duplicate commit hits were omitted.
--concurrency or GITHUB_PULL_CONCURRENCY bounds in-flight GETs (default
DEFAULT_CONCURRENCY, max MAX_CONCURRENCY). Pages inside one collection
stay in order. Independent collections, detail GETs, reviews, and
per-branch commit lists run together. One shared gate waits out
Retry-After, a primary rate-limit reset, or a secondary rate limit.
--readable PATH chooses the summary. --no-readable skips it. --readable -
prints the summary on stdout and requires --output. Progress goes to stderr.

Intentional omissions are documented in the service README. Discussions
and Projects v2 have no REST collection.

The webhook receiver does not use this script. GITHUB_TEST_REPO is the
test target only; nothing here filters live deliveries.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from envelope import wrap_github

API = "https://api.github.com"
USER_AGENT = "ember-github-ingestion"
MAX_PAGES = 200
PER_PAGE = 100
DEFAULT_CONCURRENCY = 32
MAX_CONCURRENCY = 80
MAX_RETRY_WAIT = 120.0
SECONDARY_RATE_LIMIT_WAIT = 60.0
Signer = Callable[[str, bytes], bytes]


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


class PullError(Exception):
    pass


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def openssl_sign(pem_path: str, data: bytes) -> bytes:
    """RS256 signature. Python's stdlib has no RSA signer."""
    try:
        completed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem_path],
            input=data,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PullError(
            "openssl is required to sign the GitHub App JWT (RS256). "
            "No Python crypto package is installed."
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise PullError(f"openssl failed to sign the App JWT: {detail}")
    return completed.stdout


def build_app_jwt(app_id: str, pem_path: str, *, now: int, sign: Signer = openssl_sign) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - 60, "exp": now + (9 * 60), "iss": str(app_id)}
    payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{b64url(sign(pem_path, signing_input))}"


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


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def resolve_concurrency(cli_value: int | None) -> int:
    """CLI wins, then GITHUB_PULL_CONCURRENCY, then DEFAULT_CONCURRENCY."""
    if cli_value is None:
        raw = os.environ.get("GITHUB_PULL_CONCURRENCY", "").strip()
        if not raw:
            return DEFAULT_CONCURRENCY
        try:
            cli_value = int(raw)
        except ValueError as exc:
            raise PullError("GITHUB_PULL_CONCURRENCY must be an integer") from exc
    if cli_value < 1 or cli_value > MAX_CONCURRENCY:
        raise PullError(f"concurrency must be from 1 to {MAX_CONCURRENCY}")
    return cli_value


def retry_after_seconds(response: Response, *, now: float | None = None) -> float | None:
    """Seconds to wait on a GitHub throttle, or None when the response is final."""
    if response.status < 400:
        return None
    headers = {key.lower(): value for key, value in response.headers.items()}
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    if response.status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        reset = headers.get("x-ratelimit-reset")
        if reset:
            try:
                clock = time.time() if now is None else now
                return max(0.0, float(reset) - clock)
            except ValueError:
                return None
    if response.status in (403, 429):
        text = response.body.decode("utf-8", "replace").lower()
        if "secondary rate limit" in text or "abuse detection" in text:
            return SECONDARY_RATE_LIMIT_WAIT
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
    gate: RateLimitGate | None = None,
) -> Response:
    """Repeat when GitHub sends Retry-After, a primary reset, or a secondary limit.

    ``gate`` is shared by every worker in one pull. A throttle extends it so
    the next request from any worker waits out the same window.
    """
    response: Response | None = None
    for attempt in range(attempts):
        if gate is not None:
            gate.wait()
        response = exchange(method, url, headers, body)
        wait = retry_after_seconds(response)
        if wait is None or attempt == attempts - 1:
            return response
        wait = min(wait, MAX_RETRY_WAIT)
        if gate is not None:
            gate.extend(wait)
        print(f"GitHub asked to wait {wait:.0f}s ({response.status}) {url}", file=sys.stderr)
        sleep(wait)
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


def _private_key_file() -> tuple[str, Callable[[], None]]:
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if path:
        if not os.path.isfile(path):
            raise PullError(f"GITHUB_APP_PRIVATE_KEY_PATH is not a file: {path}")
        return path, lambda: None
    pem = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    if "BEGIN" not in pem:
        raise PullError(
            "Set GITHUB_APP_PRIVATE_KEY_PATH to the App .pem file, "
            "or GITHUB_APP_PRIVATE_KEY to the PEM contents."
        )
    handle = tempfile.NamedTemporaryFile(prefix="ember-github-app-", suffix=".pem", delete=False)
    os.chmod(handle.name, 0o600)
    text = pem if pem.endswith("\n") else pem + "\n"
    handle.write(text.encode("utf-8"))
    handle.close()
    return handle.name, lambda: os.remove(handle.name)


def _headers(token: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _read_json(response: Response, what: str):
    if response.status >= 400:
        detail = response.body.decode("utf-8", "replace")[:500]
        raise PullError(f"{what} failed ({response.status}): {detail}")
    return response.json()


def installation_token(app_jwt: str, repo: str, exchange, gate: RateLimitGate | None = None) -> str:
    found = exchange_with_retry(
        exchange,
        "GET",
        f"{API}/repos/{repo}/installation",
        _headers(app_jwt),
        None,
        gate=gate,
    )
    installation = _read_json(found, f"installation lookup for {repo}")
    installation_id = installation.get("id")
    if not isinstance(installation_id, int):
        raise PullError(f"installation lookup for {repo} did not return an id")
    minted = exchange_with_retry(
        exchange,
        "POST",
        f"{API}/app/installations/{installation_id}/access_tokens",
        _headers(app_jwt, json_body=True),
        b"{}",
        gate=gate,
    )
    token = _read_json(minted, "installation token").get("token")
    if not isinstance(token, str) or not token:
        raise PullError("installation token response had no token")
    return token


# JSONL order. The readable summary uses the same sequence.
ENTITY_KINDS = (
    "repository",
    "label",
    "milestone",
    "issue",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "branch",
    "commit",
    "commit_comment",
    "release",
    "tag",
    "annotated_tag",
    "issue_event",
    "contributor",
)

HEADINGS = {
    "repository": "Repository",
    "label": "Labels",
    "milestone": "Milestones",
    "issue": "Issues",
    "issue_comment": "Issue comments",
    "pull_request": "Pull requests",
    "pull_request_review": "Pull request reviews",
    "pull_request_review_comment": "Pull request review comments",
    "branch": "Branches",
    "commit": "Commits",
    "commit_comment": "Commit comments",
    "release": "Releases",
    "tag": "Tags",
    "annotated_tag": "Annotated tags",
    "issue_event": "Issue events",
    "contributor": "Contributors",
}


class Capture:
    """Records kept beside the JSONL so the summary can name each object."""

    def __init__(self, records: list[tuple[str, dict]], duplicate_commits: int) -> None:
        self.records = records
        self.duplicate_commits = duplicate_commits

    def envelopes(self) -> list[dict]:
        return [wrap_github(body) for _kind, body in self.records]


def _numbers(items: list[dict], label: str) -> list[int]:
    numbers: list[int] = []
    for item in items:
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise PullError(f"{label} list item had no number")
        numbers.append(number)
    return numbers


def _branch_name(branch: dict) -> str:
    name = branch.get("name")
    if not isinstance(name, str) or not name:
        raise PullError("branch had no name")
    return name


def order_branches(branches: list[dict], default_branch: str) -> list[dict]:
    """Default branch first, then the rest by name. Tip refs stay in this order."""
    return sorted(branches, key=lambda branch: (_branch_name(branch) != default_branch, _branch_name(branch)))


def _annotated_shas(refs: list[dict]) -> list[str]:
    shas: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        obj = ref.get("object")
        if not isinstance(obj, dict) or obj.get("type") != "tag":
            continue
        sha = obj.get("sha")
        if isinstance(sha, str) and sha and sha not in seen:
            seen.add(sha)
            shas.append(sha)
    return shas


def unique_commits(groups: list[list[dict]]) -> tuple[list[dict], int]:
    """First SHA wins. Later branch and pull-request hits count as duplicates."""
    seen: set[str] = set()
    unique: list[dict] = []
    duplicates = 0
    for group in groups:
        for commit in group:
            sha = commit.get("sha")
            if not isinstance(sha, str) or not sha:
                raise PullError("commit had no sha")
            if sha in seen:
                duplicates += 1
                continue
            seen.add(sha)
            unique.append(commit)
    return unique, duplicates


class GithubPull:
    """REST backfill for one repo. HTTP stays inside ``workers`` at a time."""

    def __init__(self, repo: str, token: str, exchange, workers: int, gate: RateLimitGate | None = None) -> None:
        self.repo = repo
        self.token = token
        self.exchange = exchange
        self.workers = workers
        self.gate = gate or RateLimitGate()

    def _request(self, url: str) -> Response:
        return exchange_with_retry(self.exchange, "GET", url, _headers(self.token), None, gate=self.gate)

    def get_object(self, url: str) -> dict:
        payload = _read_json(self._request(url), f"GET {url}")
        if not isinstance(payload, dict):
            raise PullError(f"expected a JSON object from {url}")
        return payload

    def get_list(self, url: str, *, allow_404: bool = False) -> list[dict]:
        found: list[dict] = []
        pages = 0
        while url:
            pages += 1
            if pages > MAX_PAGES:
                raise PullError(f"stopped after {MAX_PAGES} pages for {url}")
            response = self._request(url)
            if allow_404 and pages == 1 and response.status == 404:
                return []
            if response.status == 204 or not response.body.strip():
                return found
            payload = _read_json(response, f"GET {url}")
            if not isinstance(payload, list):
                raise PullError(f"expected a JSON list from {url}")
            for item in payload:
                if not isinstance(item, dict):
                    raise PullError(f"expected objects in {url}")
                found.append(item)
            url = parse_next_link(response.headers.get("Link") or response.headers.get("link"))
        return found

    def _full(self, collection: str, number: int) -> dict:
        """GET one issue or pull request. List rows are not what we emit."""
        payload = self.get_object(f"{API}/repos/{self.repo}/{collection}/{number}")
        if "body" not in payload:
            payload["body"] = None
        return payload

    def commits_for(self, branch: dict) -> list[dict]:
        name = urllib.parse.quote(_branch_name(branch), safe="")
        return self.get_list(f"{API}/repos/{self.repo}/commits?sha={name}&per_page={PER_PAGE}")

    def _list_urls(self) -> dict[str, str]:
        repo = f"{API}/repos/{self.repo}"
        page = f"per_page={PER_PAGE}"
        return {
            "labels": f"{repo}/labels?{page}",
            "milestones": f"{repo}/milestones?state=all&{page}",
            "issues": f"{repo}/issues?state=all&{page}",
            "issue_comments": f"{repo}/issues/comments?{page}",
            "pulls": f"{repo}/pulls?state=all&{page}",
            "review_comments": f"{repo}/pulls/comments?{page}",
            "branches": f"{repo}/branches?{page}",
            "commit_comments": f"{repo}/comments?{page}",
            "releases": f"{repo}/releases?{page}",
            "tags": f"{repo}/tags?{page}",
            "issue_events": f"{repo}/issues/events?{page}",
            "contributors": f"{repo}/contributors?{page}",
        }

    def _fetch_named(self, names: list[str]) -> dict[str, object]:
        urls = self._list_urls()

        def fetch(name: str):
            if name == "repository":
                return self.get_object(f"{API}/repos/{self.repo}")
            if name == "tag_refs":
                return self.get_list(
                    f"{API}/repos/{self.repo}/git/matching-refs/tags?per_page={PER_PAGE}",
                    allow_404=True,
                )
            return self.get_list(urls[name])

        values = map_ordered(fetch, names, self.workers)
        return dict(zip(names, values))

    def capture(self, progress: Callable[[str], None] | None = None) -> Capture:
        """Fetch every collection, then detail GETs, and return records in JSONL order."""
        if progress is None:
            progress = lambda _message: None
        progress("listing repository, issues, pull requests, refs, and comments")
        names = ["repository", *self._list_urls().keys(), "tag_refs"]
        fetched = self._fetch_named(names)
        repo = fetched["repository"]
        if not isinstance(repo, dict):
            raise PullError("repository payload was not an object")
        default_branch = repo.get("default_branch")
        if not isinstance(default_branch, str):
            default_branch = ""

        def listed(name: str) -> list[dict]:
            value = fetched[name]
            if not isinstance(value, list):
                raise PullError(f"{name} payload was not a list")
            return value

        issue_numbers = _numbers(listed("issues"), "issue")
        pull_numbers = _numbers(listed("pulls"), "pull")
        branches = order_branches(listed("branches"), default_branch)
        annotated = _annotated_shas(listed("tag_refs"))
        progress("fetching details, reviews, and commits")

        jobs: list[tuple[str, Callable[[], object]]] = []
        for number in issue_numbers:
            jobs.append(("issue", lambda number=number: self._full("issues", number)))
        for number in pull_numbers:
            jobs.append(("pull", lambda number=number: self._full("pulls", number)))
        for number in pull_numbers:
            jobs.append(
                (
                    "reviews",
                    lambda number=number: self.get_list(
                        f"{API}/repos/{self.repo}/pulls/{number}/reviews?per_page={PER_PAGE}"
                    ),
                )
            )
        for branch in branches:
            jobs.append(("commits", lambda branch=branch: self.commits_for(branch)))
        for number in pull_numbers:
            jobs.append(
                (
                    "pr_commits",
                    lambda number=number: self.get_list(
                        f"{API}/repos/{self.repo}/pulls/{number}/commits?per_page={PER_PAGE}"
                    ),
                )
            )
        for sha in annotated:
            jobs.append(("annotated", lambda sha=sha: self.get_object(f"{API}/repos/{self.repo}/git/tags/{sha}")))

        results = map_ordered(lambda job: job[1](), jobs, self.workers)
        cursor = 0

        def take(count: int) -> list:
            nonlocal cursor
            chunk = results[cursor : cursor + count]
            cursor += count
            return chunk

        issues = take(len(issue_numbers))
        pulls = take(len(pull_numbers))
        review_lists = take(len(pull_numbers))
        commit_groups = take(len(branches))
        pr_commit_groups = take(len(pull_numbers))
        annotated_objects = take(len(annotated))
        commits, duplicates = unique_commits([*commit_groups, *pr_commit_groups])

        records: list[tuple[str, dict]] = [("repository", repo)]
        records += [("label", item) for item in listed("labels")]
        records += [("milestone", item) for item in listed("milestones")]
        records += [("issue", item) for item in issues]
        records += [("issue_comment", item) for item in listed("issue_comments")]
        records += [("pull_request", item) for item in pulls]
        for reviews in review_lists:
            records += [("pull_request_review", item) for item in reviews]
        records += [("pull_request_review_comment", item) for item in listed("review_comments")]
        records += [("branch", item) for item in branches]
        records += [("commit", item) for item in commits]
        records += [("commit_comment", item) for item in listed("commit_comments")]
        records += [("release", item) for item in listed("releases")]
        records += [("tag", item) for item in listed("tags")]
        records += [("annotated_tag", item) for item in annotated_objects]
        records += [("issue_event", item) for item in listed("issue_events")]
        records += [("contributor", item) for item in listed("contributors")]
        progress(f"captured {len(records)} envelopes; duplicate commit hits omitted: {duplicates}")
        return Capture(records, duplicates)


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


def _state_label(body: dict) -> str:
    if body.get("merged_at"):
        return "merged"
    state = body.get("state")
    if isinstance(state, str) and state:
        return state
    return "unknown"


def _body_excerpt(body: dict, limit: int = 80) -> str:
    text = body.get("body")
    if text is None:
        text = body.get("message")
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    if not flat:
        return ""
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _login(body: dict) -> str:
    login = body.get("login")
    if isinstance(login, str) and login:
        return login
    user = body.get("user")
    if isinstance(user, dict):
        nested = user.get("login")
        if isinstance(nested, str) and nested:
            return nested
    return "unknown"


def _trailing_number(url: object) -> str:
    if not isinstance(url, str) or not url:
        return "?"
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else "?"


def _short_sha(sha: object) -> str:
    if not isinstance(sha, str) or not sha:
        return "?"
    return sha[:7]


def _commit_subject(body: dict) -> str:
    commit = body.get("commit")
    if not isinstance(commit, dict):
        return ""
    message = commit.get("message")
    if not isinstance(message, str) or not message:
        return ""
    return message.splitlines()[0]


def _with_excerpt(line: str, body: dict) -> str:
    excerpt = _body_excerpt(body)
    if excerpt:
        return f"{line} — {excerpt}"
    return line


def _summary_line(kind: str, body: dict) -> str:
    if kind == "repository":
        name = body.get("full_name") if isinstance(body.get("full_name"), str) else ""
        branch = body.get("default_branch") if isinstance(body.get("default_branch"), str) else ""
        return f"- {name} default `{branch}`".rstrip()
    if kind == "label":
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        return f"- {name}".rstrip()
    if kind == "milestone":
        title = body.get("title") if isinstance(body.get("title"), str) else ""
        return f"- #{body.get('number')} {_state_label(body)} — {title}".rstrip()
    if kind in ("issue", "pull_request"):
        title = body.get("title") if isinstance(body.get("title"), str) else ""
        line = f"- #{body.get('number')} {_state_label(body)} — {title}".rstrip()
        return _with_excerpt(line, body)
    if kind == "issue_comment":
        line = f"- issue #{_trailing_number(body.get('issue_url'))} comment {body.get('id')} by {_login(body)}"
        return _with_excerpt(line, body)
    if kind == "pull_request_review":
        state = body.get("state") if isinstance(body.get("state"), str) else ""
        line = (
            f"- pull #{_trailing_number(body.get('pull_request_url'))} "
            f"review {body.get('id')} {state} by {_login(body)}"
        ).rstrip()
        return _with_excerpt(line, body)
    if kind == "pull_request_review_comment":
        line = (
            f"- pull #{_trailing_number(body.get('pull_request_url'))} "
            f"comment {body.get('id')} by {_login(body)}"
        )
        return _with_excerpt(line, body)
    if kind == "branch":
        commit = body.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        return f"- `{_branch_name(body)}` at `{_short_sha(sha)}`"
    if kind == "commit":
        return f"- `{_short_sha(body.get('sha'))}` {_commit_subject(body)}".rstrip()
    if kind == "commit_comment":
        line = f"- `{_short_sha(body.get('commit_id'))}` comment {body.get('id')} by {_login(body)}"
        return _with_excerpt(line, body)
    if kind == "release":
        tag = body.get("tag_name") if isinstance(body.get("tag_name"), str) else ""
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        line = f"- {tag} — {name}" if name else f"- {tag}"
        return _with_excerpt(line, body)
    if kind == "tag":
        commit = body.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        return f"- {name} at `{_short_sha(sha)}`"
    if kind == "annotated_tag":
        name = body.get("tag") if isinstance(body.get("tag"), str) else ""
        line = f"- {name} `{_short_sha(body.get('sha'))}`".rstrip()
        return _with_excerpt(line, body)
    if kind == "issue_event":
        event = body.get("event") if isinstance(body.get("event"), str) else ""
        return f"- {body.get('id')} {event}".rstrip()
    if kind == "contributor":
        contributions = body.get("contributions")
        return f"- {_login(body)} ({contributions})"
    return f"- {kind}"


def render_summary(repo: str, records: list[tuple[str, dict]], *, duplicate_commits: int = 0) -> str:
    """Counts and keys for humans. Does not replace the JSONL intake.

    ``records`` are ``(kind, raw object)`` pairs in JSONL order. Commit rows
    are the unique objects. ``duplicate_commits`` is the number of later
    branch or pull-request hits that were not written.
    """
    grouped: dict[str, list[str]] = {kind: [] for kind in ENTITY_KINDS}
    extra: list[str] = []
    for kind, body in records:
        line = _summary_line(kind, body)
        if kind in grouped:
            grouped[kind].append(line)
        else:
            extra.append(line)
    commit_count = len(grouped["commit"])
    parts = [
        f"# {repo}",
        "",
        f"Envelopes: {len(records)}.",
        f"Unique commits: {commit_count}. Duplicate SHA hits omitted: {duplicate_commits}.",
        "Issue comments include conversation comments on pull requests.",
        "Review comments are diff line comments. Branch rows are tip refs.",
        "A pull request is stored twice: the issue resource and the pull resource.",
        "",
        "## Counts",
        "",
        "| Entity | Envelopes |",
        "| --- | ---: |",
    ]
    for kind in ENTITY_KINDS:
        parts.append(f"| {HEADINGS[kind]} | {len(grouped[kind])} |")
    if extra:
        parts.append(f"| Other | {len(extra)} |")
    parts.append("")
    for kind in ENTITY_KINDS:
        parts.append(f"## {HEADINGS[kind]}")
        parts.append("")
        parts.append("\n".join(grouped[kind]) if grouped[kind] else "- (none)")
        parts.append("")
    if extra:
        parts.append("## Other")
        parts.append("")
        parts.append("\n".join(extra))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def pull(
    repo: str,
    app_id: str,
    pem_path: str,
    exchange,
    *,
    now: int | None = None,
    sign: Signer = openssl_sign,
    workers: int = DEFAULT_CONCURRENCY,
    progress: Callable[[str], None] | None = None,
) -> Capture:
    if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise PullError("GITHUB_TEST_REPO must look like owner/name")
    now = int(time.time()) if now is None else now
    app_jwt = build_app_jwt(app_id, pem_path, now=now, sign=sign)
    gate = RateLimitGate()
    token = installation_token(app_jwt, repo, exchange, gate)
    return GithubPull(repo, token, exchange, workers, gate).capture(progress)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Full REST backfill of one test repo into EMBER-39 JSONL. "
            "With --output, also write counts and keys at <output>.readable.md."
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
    parser.add_argument("--repo", help="owner/name. Defaults to GITHUB_TEST_REPO.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"In-flight GitHub requests (default {DEFAULT_CONCURRENCY}, "
        f"or GITHUB_PULL_CONCURRENCY, max {MAX_CONCURRENCY}). "
        "Pages inside one collection stay in order. A shared gate waits out rate limits.",
    )
    args = parser.parse_args(argv)
    if args.no_readable and args.readable:
        print("Use only one of --readable or --no-readable.", file=sys.stderr)
        return 2
    if args.readable == "-" and not args.output:
        print("--readable - needs --output so JSONL is not mixed into stdout.", file=sys.stderr)
        return 2

    repo = (args.repo or os.environ.get("GITHUB_TEST_REPO", "")).strip()
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    if not app_id:
        print("Set GITHUB_APP_ID.", file=sys.stderr)
        return 2
    if not repo:
        print("Set GITHUB_TEST_REPO or pass --repo owner/name.", file=sys.stderr)
        return 2

    pem_path, cleanup = _private_key_file()
    try:
        workers = resolve_concurrency(args.concurrency)

        def log(message: str) -> None:
            print(message, file=sys.stderr)

        capture = pull(repo, app_id, pem_path, urllib_exchange, workers=workers, progress=log)
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
            summary = render_summary(repo, capture.records, duplicate_commits=capture.duplicate_commits)
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
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
