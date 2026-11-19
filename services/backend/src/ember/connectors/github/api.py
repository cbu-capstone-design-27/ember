"""The GitHub REST calls enrich makes: an installation token, then pages of one resource.

The retry and rate-limit rules are core/http.py's, with GitHub's own limits added: a 403 that is a
limit, not a permission error, and the secondary limit's wait (github-webhooks.md, "Rate limits").
"""

from __future__ import annotations

import time

import jwt

from ember.core import http
from ember.core.errors import Permanent, Transient
from ember.core.http import Exchange, Response

API = "https://api.github.com"
MAX_PAGES = 200
SECONDARY_RATE_LIMIT_WAIT = 60.0


class GitHubLimits(http.Limits):
    name = "GitHub"

    def is_rate_limited(self, response: Response) -> bool:
        """A primary or secondary limit, not a permission 403."""
        if response.status == 429:
            return True
        if response.status != 403:
            return False
        return bool(response.lowered().get("retry-after")) or http.remaining(response) == 0 or _is_secondary(response)

    def backoff(self, response: Response, attempt: int) -> float:
        if _is_secondary(response):
            return min(SECONDARY_RATE_LIMIT_WAIT * 2 ** max(0, attempt), http.MAX_BACKOFF_WAIT)
        return super().backoff(response, attempt)


LIMITS = GitHubLimits()


def _is_secondary(response: Response) -> bool:
    text = response.body.decode("utf-8", "replace").lower()
    return "secondary rate limit" in text or "abuse detection" in text


def app_jwt(app_id: str, private_key: str, *, now: int | None = None) -> str:
    """The GitHub App's own JWT (RS256), good for 10 minutes; GitHub allows 60 s of clock drift back."""
    issued = int(time.time()) if now is None else now
    return jwt.encode({"iat": issued - 60, "exp": issued + 9 * 60, "iss": str(app_id)}, private_key, algorithm="RS256")


def headers(token: str, *, json_body: bool = False) -> dict[str, str]:
    out = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "User-Agent": http.USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
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


def installation_token(jwt_token: str, repo: str, exchange: Exchange) -> str:
    """Look up the repo's installation with the App JWT, then mint that installation a token."""
    found = http.exchange_with_retry(exchange, "GET", f"{API}/repos/{repo}/installation", headers(jwt_token), None,
                                     limits=LIMITS)
    installation_id = http.read_json(found, f"installation lookup for {repo}", LIMITS).get("id")
    if not isinstance(installation_id, int):
        raise Permanent(f"installation lookup for {repo} returned no id")
    return mint_token(jwt_token, installation_id, exchange)


def mint_token(jwt_token: str, installation_id: int | str, exchange: Exchange) -> str:
    """A token for an installation whose id is known, as backfill:github has from `installations`."""
    minted = http.exchange_with_retry(exchange, "POST", f"{API}/app/installations/{installation_id}/access_tokens",
                                      headers(jwt_token, json_body=True), b"{}", limits=LIMITS)
    token = http.read_json(minted, "installation token", LIMITS).get("token")
    if not isinstance(token, str) or not token:
        raise Transient("installation token response had no token")
    return token
