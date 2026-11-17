"""enrich:github (EMBER-65): turns GitHub rows in raw_events into source_records.

What each event becomes and what it fetches is docs/ingestion/github-webhooks.md. Moved from
services/github-ingestion/enrich.py onto QueueWorker (EMBER-160), which brings:

- claiming in the source's order, `occurred_at` then `id`, and copying `raw_events.occurred_at` to
  `source_records.occurred_at` (EMBER-63, architecture.md "Source ordering value"). Intake sets it
  from the payload with the doc's rules, so the worker no longer recomputes it;
- the runtime's failure classes: a rate limit sets the row aside until GitHub allows the next call,
  a 5xx or network error backs off, and a 404 or permission error sets it aside for an hour.

Unchanged: mapped rows are claimed BATCH at a time, one savepoint per row. Before each round every
claimable row whose event isn't mapped (CI events and the like) is marked enriched in one statement
(EMBER-92). File lists and compares are fetched once per head and kept in a small LRU. Every call
waits on and updates the shared `rate_limits` row for the installation (work/rate_limits.py), so
replicas and the backfill respect one limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ember.connectors.github import api
from ember.core import http
from ember.core.db import Database
from ember.core.errors import Permanent
from ember.core.models import RawEvent, SourceRecord
from ember.work.queue_worker import QueueWorker
from ember.work.rate_limits import SharedRateLimit

LOG = logging.getLogger("ember.enrich.github")

SOURCE = "github"
FULL_PUSH = 2048  # GitHub's cap on a push payload's commits array
PR_FILE_ACTIONS = ("opened", "reopened", "synchronize")
TOKEN_TTL = 50 * 60  # installation tokens last an hour
INSERT_CHUNK = 1000
FETCH_CACHE = 500  # file lists and compares kept, least recently used dropped

# event: (kind, payload key of the object, object_id pattern, version field)
OBJECTS = {
    "issues": ("issue", "issue", "{repo}#{number}", "updated_at"),
    "issue_comment": ("issue_comment", "comment", "{repo}/issue-comment/{id}", "updated_at"),
    "pull_request": ("pull_request", "pull_request", "{repo}#{number}", "updated_at"),
    "pull_request_review": ("pull_request_review", "review", "{repo}/review/{id}", "submitted_at"),
    "pull_request_review_comment": ("pull_request_review_comment", "comment", "{repo}/review-comment/{id}", "updated_at"),
    "commit_comment": ("commit_comment", "comment", "{repo}/commit-comment/{id}", "updated_at"),
    "milestone": ("milestone", "milestone", "{repo}/milestone/{number}", "updated_at"),
    "release": ("release", "release", "{repo}/release/{id}", "published_at"),
    "label": ("label", "label", "{repo}/label/{id}", None),
    "repository": ("repository", "repository", "{id}", None),
}
# Events to_records handles. Every other github row is marked enriched in bulk, never claimed.
# installation* make no record yet but stay per-row work (github-webhooks.md, "Event types")
MAPPED = (*OBJECTS, "push", "create", "delete", "installation", "installation_repositories")
# Versions the doc sets to <received_at> because the object has no usable time of its own
RECEIVED_VERSION = {("pull_request_review", "dismissed"), ("release", "edited"), ("release", "deleted")}


def _full_ref(body: dict) -> str:
    """create/delete send a short ref plus ref_type; push sends refs/heads/... Fold both into one object."""
    ref = body["ref"]
    if ref.startswith("refs/"):
        return ref
    return f"refs/{'tags' if body.get('ref_type') == 'tag' else 'heads'}/{ref}"


def to_records(raw: RawEvent, fetched: dict) -> list[dict]:
    """The source_records rows one delivery becomes: kind, object_id, version and record."""
    event, _, action = raw.event_name.partition(":")
    body = raw.envelope["body"]
    repo = (body.get("repository") or {}).get("full_name")
    received = raw.received_at.isoformat()
    if event not in MAPPED:
        return []

    def row(kind: str, object_id: str, version: str, payload: dict, tombstone: bool = False,
            extras: dict | None = None) -> dict:
        record: dict = {"payload": payload}
        if extras:
            record["fetched"] = extras
        if tombstone:
            record["tombstone"] = True
        return {"kind": kind, "object_id": object_id, "version": version, "record": record}

    if event == "push":
        commits = fetched["compare"] if "compare" in fetched else body.get("commits") or []
        rows = [row("commit", f"{repo}@{c.get('id') or c['sha']}", c.get("id") or c["sha"], c) for c in commits]
        ref = {key: value for key, value in body.items() if key != "commits"}
        rows.append(row("ref", f"{repo}:{body['ref']}", body["after"], ref))
        return rows
    if event == "create":
        return [row("ref", f"{repo}:{_full_ref(body)}", f"created:{received}", body)]
    if event == "delete":
        return [row("ref", f"{repo}:{_full_ref(body)}", f"deleted:{received}", body, tombstone=True)]
    if event not in OBJECTS:
        # installation and installation_repositories make no record. Updating `installations` and
        # starting or stopping reconcile is out of scope for EMBER-65
        return []

    kind, key, pattern, version_field = OBJECTS[event]
    obj = body[key]
    object_id = pattern.format(repo=repo, **obj)
    if version_field is None or (event, action) in RECEIVED_VERSION:
        version = received
    elif action == "deleted":
        version = f"deleted:{received}"
    elif event == "release":
        version = obj.get("published_at") or obj["created_at"]
    else:
        version = obj[version_field]
    return [row(kind, object_id, version, body, tombstone=action == "deleted", extras=fetched)]


class GitHubEnrich(QueueWorker[RawEvent]):
    """enrich:github. `exchange` is the GitHub boundary tests replace; `app_jwt` mints the App's JWT."""

    channel = "raw_events"
    model = RawEvent
    connections_per_job = 2  # the claim's transaction, and a short one for rate_limits

    def __init__(self, db: Database, *, app_jwt: Callable[[], str], exchange: http.Exchange = http.urllib_exchange,
                 concurrency: int = 4, batch: int = 25, poll_seconds: float = 30.0) -> None:
        super().__init__(db, name="enrich:github", concurrency=concurrency, batch=batch, poll_seconds=poll_seconds)
        self.app_jwt = app_jwt
        self.exchange = exchange
        self.limits = SharedRateLimit(SOURCE, api.LIMITS, lambda: self.sessions)
        self.tokens: dict[str, tuple[str, float]] = {}
        self.fetched: OrderedDict[tuple, asyncio.Future] = OrderedDict()
        self.skipped_unmapped = 0

    def claimable(self) -> tuple:
        return (RawEvent.source == SOURCE, RawEvent.enriched_at.is_(None), RawEvent.tenant.is_not(None))

    def pending(self) -> Select:
        return (select(RawEvent)
                .where(*self.claimable(), func.split_part(RawEvent.event_name, ":", 1).in_(MAPPED))
                .order_by(RawEvent.occurred_at, RawEvent.id))

    async def drain(self, stopping: asyncio.Event) -> None:
        await self.skip_unmapped()
        await super().drain(stopping)

    async def skip_unmapped(self) -> None:
        """Mark every claimable row whose event isn't mapped enriched, in one statement."""
        assert self.sessions is not None
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(RawEvent)
                .where(*self.claimable(), func.split_part(RawEvent.event_name, ":", 1).not_in(MAPPED))
                .values(enriched_at=func.clock_timestamp())
            )
        if result.rowcount:
            self.skipped_unmapped += result.rowcount
            LOG.info("skipped %d unmapped github events", result.rowcount)

    def status(self) -> dict:
        return {**super().status(), "skipped_unmapped": self.skipped_unmapped}

    async def process(self, session: AsyncSession, raw: RawEvent) -> None:
        """Write the row's source_records and set enriched_at, in the claim's transaction."""
        rows = to_records(raw, await self.fetch_extras(raw))
        values = [{**row, "source": SOURCE, "tenant": raw.tenant, "occurred_at": raw.occurred_at, "raw_event_id": raw.id}
                  for row in rows]
        for start in range(0, len(values), INSERT_CHUNK):
            statement = insert(SourceRecord).values(values[start : start + INSERT_CHUNK])
            await session.execute(statement.on_conflict_do_nothing(index_elements=["source", "tenant", "object_id", "version"]))
        # now() is the transaction's start, the claim, before any API call; clock_timestamp() is the write
        raw.enriched_at = func.clock_timestamp()

    async def fetch_extras(self, raw: RawEvent) -> dict:
        """Only what the payload lacks (github-webhooks.md, "Event types")."""
        event, _, action = raw.event_name.partition(":")
        body = raw.envelope["body"]
        if event not in ("push", "pull_request"):
            return {}
        repo = body["repository"]["full_name"]  # both carry one; installation* events don't
        if event == "push" and len(body.get("commits") or []) >= FULL_PUSH and body["before"].strip("0"):
            # A new branch (before = 000...) has no base to compare against, so it keeps the payload's commits
            path = f"/compare/{body['before']}...{body['after']}"
            pages = await self.cached_pages(raw, (raw.tenant, repo, path), path)
            return {"compare": [commit for page in pages for commit in page.get("commits", [])]}
        if event == "pull_request" and action in PR_FILE_ACTIONS:
            pull = body["pull_request"]
            path = f"/pulls/{pull['number']}/files"
            pages = await self.cached_pages(raw, (raw.tenant, repo, path, pull["head"]["sha"]), path)
            return {"files": [item for page in pages for item in page]}
        return {}

    async def cached_pages(self, raw: RawEvent, key: tuple, path: str) -> list:
        """get_pages once per key, shared by every row and job that asks for it. A failed fetch isn't kept."""
        future = self.fetched.get(key)
        if future is None:
            future = self.fetched[key] = asyncio.ensure_future(self.get_pages(raw, path))
            if len(self.fetched) > FETCH_CACHE:
                self.fetched.popitem(last=False)
        else:
            self.fetched.move_to_end(key)
        try:
            return await asyncio.shield(future)  # one job cancelled at shutdown doesn't cancel it for the others
        except Exception:
            if self.fetched.get(key) is future:
                del self.fetched[key]
            raise

    async def get_pages(self, raw: RawEvent, path: str) -> list:
        body = raw.envelope["body"]
        repo = body["repository"]["full_name"]
        bucket = raw.account_id or str(body["installation"]["id"])
        exchange = self.limits.exchange(raw.tenant, bucket, self.exchange)
        token = await self.token(bucket, repo, exchange)
        url: str | None = f"{api.API}/repos/{repo}{path}?per_page=100"
        pages = []
        while url and len(pages) < api.MAX_PAGES:
            response = await asyncio.to_thread(http.exchange_with_retry, exchange, "GET", url, api.headers(token), None,
                                               limits=api.LIMITS)
            pages.append(http.read_json(response, f"GET {path}", api.LIMITS))
            url = api.parse_next_link(response.headers.get("Link") or response.headers.get("link"))
        return pages

    async def token(self, bucket: str, repo: str, exchange: http.Exchange) -> str:
        cached = self.tokens.get(bucket)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        jwt_token = self.app_jwt()
        token = await asyncio.to_thread(api.installation_token, jwt_token, repo, exchange)
        self.tokens[bucket] = (token, time.monotonic() + TOKEN_TTL)
        return token


def missing_app(reason: str) -> Callable[[], str]:
    """The app_jwt of a worker with no GitHub App: rows that need a call are set aside, the rest still go."""
    def app_jwt() -> str:
        raise Permanent(f"{reason}: this event needs a GitHub API call")
    return app_jwt
