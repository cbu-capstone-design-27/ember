"""Local GitHub App pull for one test repository.

Mints an App JWT (RS256 via the openssl CLI — not a Python dependency),
exchanges it for an installation token, and writes one EMBER-39 envelope
per issue, pull request, and commit.

Machine intake is JSONL, one object per line:
  {"type":"github","body":<opaque raw GitHub API object>}
Stdout is that JSONL. --output FILE writes the same JSONL and, by default,
a glance summary at FILE.readable.md (issues, pull requests, commits).
--readable PATH chooses the summary file. --no-readable skips it.
--readable - prints the summary on stdout and requires --output so the
JSONL stays a file. Progress goes to stderr.

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
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

from envelope import wrap_github

API = "https://api.github.com"
USER_AGENT = "ember-github-ingestion"
MAX_PAGES = 50
Signer = Callable[[str, bytes], bytes]


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


def installation_token(app_jwt: str, repo: str, exchange) -> str:
    found = exchange(
        "GET",
        f"{API}/repos/{repo}/installation",
        _headers(app_jwt),
        None,
    )
    installation = _read_json(found, f"installation lookup for {repo}")
    installation_id = installation.get("id")
    if not isinstance(installation_id, int):
        raise PullError(f"installation lookup for {repo} did not return an id")
    minted = exchange(
        "POST",
        f"{API}/app/installations/{installation_id}/access_tokens",
        _headers(app_jwt, json_body=True),
        b"{}",
    )
    token = _read_json(minted, "installation token").get("token")
    if not isinstance(token, str) or not token:
        raise PullError("installation token response had no token")
    return token


def iter_pages(url: str, token: str, exchange) -> Iterator[dict]:
    pages = 0
    while url:
        pages += 1
        if pages > MAX_PAGES:
            raise PullError(f"stopped after {MAX_PAGES} pages for {url}")
        response = exchange("GET", url, _headers(token), None)
        payload = _read_json(response, f"GET {url}")
        if not isinstance(payload, list):
            raise PullError(f"expected a JSON list from {url}")
        for item in payload:
            if not isinstance(item, dict):
                raise PullError(f"expected objects in {url}")
            yield item
        url = parse_next_link(response.headers.get("Link") or response.headers.get("link"))


def iter_envelopes(repo: str, token: str, exchange) -> Iterator[dict]:
    """Raw list objects from Issues, Pulls, and Commits. No cleaning."""
    resources = (
        f"{API}/repos/{repo}/issues?state=all&per_page=100",
        f"{API}/repos/{repo}/pulls?state=all&per_page=100",
        f"{API}/repos/{repo}/commits?per_page=100",
    )
    for url in resources:
        for item in iter_pages(url, token, exchange):
            yield wrap_github(item)


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


def _kind(body: dict) -> str | None:
    if isinstance(body.get("commit"), dict) and isinstance(body.get("sha"), str):
        return "commit"
    number = body.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    if "pull_request" in body or "merged_at" in body:
        return "pull"
    return "issue"


def _state_label(body: dict) -> str:
    if body.get("merged_at"):
        return "merged"
    state = body.get("state")
    if isinstance(state, str) and state:
        return state
    return "unknown"


def _summary_line(kind: str, body: dict) -> str:
    if kind == "commit":
        message = str(body["commit"].get("message") or "").splitlines()
        subject = message[0] if message else ""
        return f"- `{body['sha'][:7]}` {subject}".rstrip()
    title = body.get("title") if isinstance(body.get("title"), str) else ""
    return f"- #{body['number']} {_state_label(body)} — {title}".rstrip()


def render_summary(repo: str, envelopes: list[dict]) -> str:
    """Grouped glance list. Does not replace the JSONL intake.

    A pull request listed by both the Issues API and the Pulls API is shown
    once. The later object wins so the Pulls API state is the one you see.
    """
    order = ("issue", "pull", "commit")
    seen: dict[str, dict] = {kind: {} for kind in order}
    keys: dict[str, list] = {kind: [] for kind in order}
    for envelope in envelopes:
        body = envelope.get("body")
        if not isinstance(body, dict):
            continue
        kind = _kind(body)
        if kind is None:
            continue
        key = body.get("sha") if kind == "commit" else body.get("number")
        if key not in seen[kind]:
            keys[kind].append(key)
        seen[kind][key] = _summary_line(kind, body)
    headings = {"issue": "Issues", "pull": "Pull requests", "commit": "Commits"}
    parts = [f"# {repo}", ""]
    for kind in order:
        parts.append(f"## {headings[kind]}")
        parts.append("")
        lines = [seen[kind][key] for key in keys[kind]]
        parts.append("\n".join(lines) if lines else "- (none)")
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
) -> Iterator[dict]:
    if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise PullError("GITHUB_TEST_REPO must look like owner/name")
    now = int(time.time()) if now is None else now
    app_jwt = build_app_jwt(app_id, pem_path, now=now, sign=sign)
    token = installation_token(app_jwt, repo, exchange)
    return iter_envelopes(repo, token, exchange)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pull one test repo into EMBER-39 JSONL. "
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
    parser.add_argument("--repo", help="owner/name. Defaults to GITHUB_TEST_REPO.")
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
        envelopes = list(pull(repo, app_id, pem_path, urllib_exchange))
        if args.output:
            output = Path(args.output)
            with output.open("w", encoding="utf-8") as handle:
                count = emit_jsonl(envelopes, handle)
            print(f"wrote {count} envelopes to {output}", file=sys.stderr)
        else:
            count = emit_jsonl(envelopes, sys.stdout)
            print(f"wrote {count} envelopes", file=sys.stderr)
        if not args.no_readable:
            summary = render_summary(repo, envelopes)
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
