"""backfill:github: reads each connected installation's issues, pull requests and their comments into raw_events
(ADR 0010 "Replay and backfill on connect", github-webhooks.md "Known gaps and catch-up", EMBER-167). The
schedule, lock, cursors and rate-limit waits are work/backfill.py's.

- **Accounts.** Connected installations (`installations`, no stored credential: the App mints each one's token).
  Without the App it doesn't start.
- **What.** Every repository the installation sees (`/installation/repositories`): its issues by oldest update
  (`/issues?since=`), and for each one its comments. A pull request in that list is read as the pull itself,
  with its submitted reviews and review comments; enrich:github adds its files, as for an opened one.
- **Rows.** Each object shaped as its webhook delivery, with no action, and delivery_id `<object id>@<version>`
  (github-webhooks.md, delivery_id), so a webhook copy and a backfill copy of one version fold into one
  source_records row.
- **Progress.** Cursor `repo:<owner>/<repo>:issues` holds the `updated_at` of the last issue stored, so a stop
  or crash costs one issue. The next read starts a second before it. RECONCILE_SINCE starts a repository with no
  cursor at that date instead of its whole history. A disconnect stops the pass at the next issue.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ember.connectors.github import api
from ember.connectors.github.enrich import OBJECTS, SOURCE, TOKEN_TTL, to_records
from ember.connectors.github.webhook import _time
from ember.core import http
from ember.core.errors import ConfigError
from ember.core.models import Installation, RawEvent
from ember.work.backfill import Backfill


class Disconnected(Exception):
    """The installation was disconnected mid-pass; the pass stops and isn't counted as done."""


def as_delivery(event_name: str, body: dict, installation: str) -> tuple[str, str, datetime, dict]:
    """(delivery_id, event_name, occurred_at, body) for one object shaped as its webhook body. to_records names
    it, so its delivery_id is the source_records row's `<object id>@<version>`."""
    body = {**body, "installation": {"id": int(installation)}}
    raw = RawEvent(event_name=event_name, envelope={"type": SOURCE, "body": body}, received_at=datetime.now(timezone.utc))
    (record,) = to_records(raw, {})
    obj = body[OBJECTS[event_name][1]]
    occurred = _time(obj.get("updated_at") or obj["submitted_at"])  # github-webhooks.md, "Ordering value"
    return f"{record['object_id']}@{record['version']}", event_name, occurred, body


class GitHubBackfill(Backfill):
    source = SOURCE
    limits_rule = api.LIMITS
    refused = ()  # no stored token to refuse; an uninstall disconnects the installation (EMBER-150)
    account_stream = "installation"

    def __init__(self, *args, app_jwt: Callable[[], str], missing: str = "", since: datetime | None = None,
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.app_jwt = app_jwt
        self.missing = missing  # why there is no App, when there isn't
        self.since = since
        self.tokens: dict[str, tuple[str, float]] = {}

    async def start(self) -> None:
        if self.missing:
            raise ConfigError(self.missing)
        # As Backfill.start, less the api ring: GitHub stores no token
        self.engine = self.db.engine(pool_size=3)  # the lock, the writes, the rate_limits rows
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def accounts(self) -> Select:
        return select(Installation.account_id, Installation.tenant).where(
            Installation.source == SOURCE, Installation.disconnected_at.is_(None))

    async def secret(self, account_id: str, tenant: str) -> dict | None:
        return {} if await self.connected(account_id, tenant) else None

    async def connected(self, installation: str, tenant: str) -> bool:
        async with self.sessions() as session:
            return await session.scalar(self.accounts().where(
                Installation.account_id == installation, Installation.tenant == tenant)) is not None

    async def account(self, installation: str, tenant: str, secret: dict, stopping: asyncio.Event) -> dict[str, Any]:
        def get(url: str):
            return self.patiently(stopping, lambda: self.get(installation, tenant, url), url.split("?")[0])

        repos = [repo for page in await self.pages(get, f"{api.API}/installation/repositories?per_page=100")
                 for repo in page["repositories"]]
        rows = 0
        for repo in repos:
            rows += await self.repository(installation, tenant, repo, get)
        return {"repositories": len(repos), "rows": rows}

    async def repository(self, installation: str, tenant: str, repo: dict, get) -> int:
        """Store the repository's issues and pull requests updated since its cursor. New rows."""
        stream = f"repo:{repo['full_name']}:issues"
        position = await self.cursor(tenant, stream)
        since = _time(position) - timedelta(seconds=1) if position else self.since  # in case `since` is exclusive
        url: str | None = f"{api.API}/repos/{repo['full_name']}/issues?state=all&sort=updated&direction=asc&per_page=100"
        if since:
            url += "&since=" + since.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = 0
        while url:
            items, url = await get(url)
            for item in items:
                bodies = await self.issue_bodies(repo, item, get)
                if not await self.connected(installation, tenant):
                    raise Disconnected(f"installation {installation} was disconnected; stopped")
                rows += await self.store(installation, tenant, [as_delivery(name, body, installation)
                                                                for name, body in bodies])
                await self.save_cursor(tenant, stream, item["updated_at"])
        return rows

    async def issue_bodies(self, repo: dict, item: dict, get) -> list[tuple[str, dict]]:
        """(event name, webhook-shaped body) for an issue or pull request and what hangs off it."""
        base = f"{api.API}/repos/{repo['full_name']}"
        number = item["number"]

        async def listed(path: str) -> list:
            return [obj for page in await self.pages(get, f"{base}{path}?per_page=100") for obj in page]

        bodies: list[tuple[str, dict]] = []
        if "pull_request" in item:  # the issues list includes pull requests; the pull has head, base and merged
            pull, _ = await get(f"{base}/pulls/{number}")
            bodies.append(("pull_request", {"pull_request": pull, "repository": repo}))
            # A pending review has no submitted_at, and only its author can see it
            bodies += [("pull_request_review", {"review": review, "pull_request": pull, "repository": repo})
                       for review in await listed(f"/pulls/{number}/reviews") if review.get("submitted_at")]
            if pull.get("review_comments"):
                bodies += [("pull_request_review_comment", {"comment": comment, "pull_request": pull, "repository": repo})
                           for comment in await listed(f"/pulls/{number}/comments")]
        else:
            bodies.append(("issues", {"issue": item, "repository": repo}))
        if item.get("comments"):
            bodies += [("issue_comment", {"comment": comment, "issue": item, "repository": repo})
                       for comment in await listed(f"/issues/{number}/comments")]
        return bodies

    @staticmethod
    async def pages(get, url: str | None) -> list:
        pages = []
        while url and len(pages) < api.MAX_PAGES:
            page, url = await get(url)
            pages.append(page)
        return pages

    async def get(self, installation: str, tenant: str, url: str) -> tuple[Any, str | None]:
        """One GET with the installation's token, through its rate_limits row: (JSON, next page URL)."""
        exchange = self.limits.exchange(tenant, installation, self.exchange)
        token = await self.token(installation, exchange)
        response = await asyncio.to_thread(http.exchange_with_retry, exchange, "GET", url, api.headers(token), None,
                                           limits=api.LIMITS)
        return (http.read_json(response, f"GET {url.split('?')[0]}", api.LIMITS),
                api.parse_next_link(response.headers.get("Link") or response.headers.get("link")))

    async def token(self, installation: str, exchange: http.Exchange) -> str:
        cached = self.tokens.get(installation)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        token = await asyncio.to_thread(api.mint_token, self.app_jwt(), installation, exchange)
        self.tokens[installation] = (token, time.monotonic() + TOKEN_TTL)
        return token
