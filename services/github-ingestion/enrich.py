"""enrich:github (EMBER-65): turns GitHub rows in raw_events into source_records.

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
        python3 enrich.py

It claims rows as docs/ingestion/architecture.md, "Interim: claiming without the job queue",
describes: LISTEN on RAW_EVENTS_CHANNEL (default raw_events), and claim once at start, on any
notification on it (the payload is ignored) and on a 30 s poll. Each claim cycle first marks every
row whose event is not in MAPPED (CI events and the like) enriched in one statement. It then claims
mapped rows BATCH (default 25) at a time; each batch is enriched and marked (`enriched_at`) in one
transaction, at most CONCURRENCY (default 4) batches at once. What each event becomes and what it
fetches is docs/ingestion/github-webhooks.md. API calls reuse pull_test.py's helpers and wait on the
shared `rate_limits` row for the installation. File lists and compares are fetched once per head
and kept in a small LRU. SIGINT or SIGTERM stops claiming, lets in-flight batches finish, and exits 0.

Needs GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH (or GITHUB_APP_PRIVATE_KEY) only for events
that fetch: pull_request opened/reopened/synchronize, and pushes with a full commits array.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import RateLimit, RawEvent, SourceRecord
from pull_test import (
    API,
    MAX_PAGES,
    Response,
    _headers,
    _private_key_file,
    _read_json,
    build_app_jwt,
    exchange_with_retry,
    installation_token,
    parse_next_link,
    is_rate_limited,
    rate_limit_wait,
    throttle_seconds,
    urllib_exchange,
)

SOURCE = "github"
CHANNEL = os.environ.get("RAW_EVENTS_CHANNEL", "raw_events")
POLL_SECONDS = 30.0
FULL_PUSH = 2048  # GitHub's cap on a push payload's commits array
PR_FILE_ACTIONS = ("opened", "reopened", "synchronize")
TOKEN_TTL = 50 * 60  # installation tokens last an hour
INSERT_CHUNK = 1000
FETCH_CACHE = 500  # file lists and compares kept in memory, least recently used dropped

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
# Events to_records handles. Every other github row is marked enriched in bulk without a claim.
# installation* make no record yet but stay per-row work (github-webhooks.md, "Event types").
MAPPED = (*OBJECTS, "push", "create", "delete", "installation", "installation_repositories")
# Versions the doc sets to <received_at> because the object has no usable time of its own
RECEIVED_VERSION = {("pull_request_review", "dismissed"), ("release", "edited"), ("release", "deleted")}


def _time(value: str | int | float) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ordering_value(raw_event: RawEvent) -> datetime:
    """The time GitHub says the event happened (github-webhooks.md, "Ordering value"). Fills occurred_at."""
    # TODO(EMBER-63): read the raw_events ordering column once it lands
    event, _, action = raw_event.event_name.partition(":")
    body = raw_event.envelope["body"]
    if action in ("deleted", "dismissed") or event in ("label", "repository", "create", "delete"):
        return raw_event.received_at
    if event == "push":
        pushed = (body.get("repository") or {}).get("pushed_at")
        return _time(pushed) if pushed else raw_event.received_at
    spec = OBJECTS.get(event)
    obj = body.get(spec[1]) if spec else None
    for field in ("updated_at", "submitted_at", "published_at", "created_at"):
        if isinstance(obj, dict) and obj.get(field):
            return _time(obj[field])
    return raw_event.received_at


def _full_ref(body: dict) -> str:
    """create/delete send a short ref plus ref_type; push sends refs/heads/... Fold both into one object."""
    ref = body["ref"]
    if ref.startswith("refs/"):
        return ref
    return f"refs/{'tags' if body.get('ref_type') == 'tag' else 'heads'}/{ref}"


def to_records(raw_event: RawEvent, fetched: dict) -> list[dict]:
    """The source_records rows one delivery becomes: kind, object_id, version and record."""
    event, _, action = raw_event.event_name.partition(":")
    body = raw_event.envelope["body"]
    repo = (body.get("repository") or {}).get("full_name")
    received = raw_event.received_at.isoformat()
    if event not in MAPPED:
        return []

    def row(kind: str, object_id: str, version: str, payload: dict, tombstone: bool = False, extras: dict | None = None) -> dict:
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
        # starting or stopping reconcile is out of scope for EMBER-65.
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


def env_app_jwt() -> str:
    path, cleanup = _private_key_file()
    try:
        return build_app_jwt(os.environ["GITHUB_APP_ID"], path, now=int(time.time()))
    finally:
        cleanup()


class Worker:
    """One enrich:github process. `exchange` and `app_jwt` are the GitHub boundary tests stub."""

    def __init__(
        self,
        database_url: str,
        *,
        exchange=urllib_exchange,
        app_jwt=env_app_jwt,
        concurrency: int = 4,
        batch: int = 25,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        url = make_url(database_url).set(drivername="postgresql+asyncpg")
        if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
            url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
        # Each in-flight batch holds one connection for its transaction and briefly a second for rate_limits
        self.engine = create_async_engine(url, pool_size=2 * concurrency + 1)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.exchange = exchange
        self.app_jwt = app_jwt
        self.poll_seconds = poll_seconds
        self.batch = batch
        self.slots = asyncio.Semaphore(concurrency)
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.tokens: dict[str, tuple[str, float]] = {}
        self.fetched: OrderedDict[tuple, asyncio.Future] = OrderedDict()
        # ponytail: failed rows are skipped until the next poll, in memory. The job queue's retry and dead letter replace it.
        self.failed: set[int] = set()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def _notified(self, _conn, _pid, _channel, _payload: str) -> None:
        # Any notification wakes the worker. The claim filters by source, so the payload is never read.
        self.wake.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        try:
            async with self.engine.connect() as conn:
                listener = (await conn.get_raw_connection()).driver_connection
                await listener.add_listener(CHANNEL, self._notified)
                print(f"enrich:github listening on {CHANNEL}", flush=True)
                while not self.stopping.is_set():
                    self.wake.clear()
                    await self._claim_until_empty()
                    try:
                        await asyncio.wait_for(self.wake.wait(), self.poll_seconds)
                    except asyncio.TimeoutError:
                        self.failed.clear()
                print(f"enrich:github stopping, finishing {len(self.tasks)} in flight", flush=True)
                await asyncio.gather(*list(self.tasks))
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.engine.dispose()

    async def _skip_unmapped(self) -> None:
        """Mark every claimable row whose event the worker doesn't map, in one statement."""
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(RawEvent)
                .where(*self._claimable(), func.split_part(RawEvent.event_name, ":", 1).not_in(MAPPED))
                .values(enriched_at=func.clock_timestamp())
            )
        if result.rowcount:
            print(f"raw_events: skipped {result.rowcount} unmapped", flush=True)

    async def _claim_until_empty(self) -> None:
        """Skip unmapped rows, then start one batch per free slot until a claim comes back short."""
        await self._skip_unmapped()
        while not self.stopping.is_set():
            await self.slots.acquire()
            claimed = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._job(claimed))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            if not await claimed:
                return

    def _claimable(self):
        return (RawEvent.source == SOURCE, RawEvent.enriched_at.is_(None), RawEvent.tenant.is_not(None))

    def _claim(self):
        query = (
            select(RawEvent)
            .where(*self._claimable(), func.split_part(RawEvent.event_name, ":", 1).in_(MAPPED))
            .order_by(RawEvent.id)
            .limit(self.batch)
            .with_for_update(skip_locked=True)
        )
        return query.where(RawEvent.id.not_in(self.failed)) if self.failed else query

    async def _job(self, claimed: asyncio.Future) -> None:
        """Claim up to BATCH rows and enrich them in one transaction. `claimed` is whether the batch was full."""
        try:
            async with self.sessions.begin() as session:
                rows = [] if self.stopping.is_set() else list(await session.scalars(self._claim()))
                claimed.set_result(len(rows) == self.batch)
                for raw in rows:
                    try:
                        # A savepoint per row, so one bad row rolls back alone and the rest still commit
                        async with session.begin_nested():
                            count = await self.enrich(session, raw)
                    except Exception as exc:  # noqa: BLE001 one bad row must not stop the batch
                        self.failed.add(raw.id)
                        print(f"raw_events {raw.id} failed, retried after the next poll: {exc!r}", file=sys.stderr, flush=True)
                    else:
                        print(f"raw_events {raw.id} {raw.event_name}: {count} new source_records", flush=True)
        except Exception as exc:  # noqa: BLE001 a failed claim or commit must not stop the worker
            if not claimed.done():
                claimed.set_result(False)
            print(f"raw_events batch failed, retried on the next claim: {exc!r}", file=sys.stderr, flush=True)
        finally:
            self.slots.release()

    async def enrich(self, session: AsyncSession, raw: RawEvent) -> int:
        """Write the row's source_records and set enriched_at, in the caller's transaction. Returns rows inserted."""
        rows = to_records(raw, await self.fetch_extras(raw))
        occurred_at = ordering_value(raw)
        values = [
            {**row, "source": SOURCE, "tenant": raw.tenant, "occurred_at": occurred_at, "raw_event_id": raw.id}
            for row in rows
        ]
        inserted = 0
        for start in range(0, len(values), INSERT_CHUNK):
            statement = insert(SourceRecord).values(values[start : start + INSERT_CHUNK])
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=["source", "tenant", "object_id", "version"])
            )
            inserted += result.rowcount
        # now() is the transaction's start, i.e. the claim, before any API call. clock_timestamp() is the write.
        raw.enriched_at = func.clock_timestamp()
        return inserted

    async def fetch_extras(self, raw: RawEvent) -> dict:
        """Only what the payload lacks (github-webhooks.md, "Event types")."""
        event, _, action = raw.event_name.partition(":")
        body = raw.envelope["body"]
        repo = body["repository"]["full_name"]
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
        """get_pages once per key, shared by every row and batch that asks for it. A failed fetch isn't kept."""
        future = self.fetched.get(key)
        if future is None:
            future = self.fetched[key] = asyncio.ensure_future(self.get_pages(raw, path))
            if len(self.fetched) > FETCH_CACHE:
                self.fetched.popitem(last=False)
        else:
            self.fetched.move_to_end(key)
        try:
            return await future
        except Exception:
            if self.fetched.get(key) is future:
                del self.fetched[key]
            raise

    async def get_pages(self, raw: RawEvent, path: str) -> list:
        body = raw.envelope["body"]
        repo = body["repository"]["full_name"]
        bucket = raw.account_id or str(body["installation"]["id"])
        exchange = self.limited_exchange(raw.tenant, bucket)
        token = await self.token(bucket, repo, exchange)
        url: str | None = f"{API}/repos/{repo}{path}?per_page=100"
        pages = []
        while url and len(pages) < MAX_PAGES:
            response = await asyncio.to_thread(
                exchange_with_retry, exchange, "GET", url, _headers(token), None
            )
            pages.append(_read_json(response, url))
            url = parse_next_link(response.headers.get("Link") or response.headers.get("link"))
        return pages

    async def token(self, bucket: str, repo: str, exchange) -> str:
        cached = self.tokens.get(bucket)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        app_jwt = await asyncio.to_thread(self.app_jwt)
        token = await asyncio.to_thread(installation_token, app_jwt, repo, exchange)
        self.tokens[bucket] = (token, time.monotonic() + TOKEN_TTL)
        return token

    def limited_exchange(self, tenant: str, bucket: str):
        """A synchronous exchange, run in a thread, that waits on and updates the rate_limits row."""
        loop = asyncio.get_running_loop()

        def exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
            asyncio.run_coroutine_threadsafe(self.wait_turn(tenant, bucket), loop).result()
            response = self.exchange(method, url, headers, body)
            asyncio.run_coroutine_threadsafe(self.record_limit(tenant, bucket, response), loop).result()
            return response

        return exchange

    def _bucket(self, tenant: str, bucket: str):
        return (RateLimit.source == SOURCE, RateLimit.tenant == tenant, RateLimit.bucket == bucket)

    async def wait_turn(self, tenant: str, bucket: str) -> None:
        async with self.sessions() as session:
            delay = await session.scalar(
                select(func.extract("epoch", RateLimit.next_allowed_at - func.now())).where(*self._bucket(tenant, bucket))
            )
        if delay is not None and delay > 0:
            await asyncio.sleep(float(delay))

    async def record_limit(self, tenant: str, bucket: str, response: Response) -> None:
        headers = {key.lower(): value for key, value in response.headers.items()}
        remaining_header = headers.get("x-ratelimit-remaining", "")
        remaining = int(remaining_header) if remaining_header.isdigit() else None
        # pull_test.py decides the wait: a throttle (429, or a 403 that is a rate limit, not a permission
        # error), else a pause when the remaining quota is at or under its floor. 5xx backoff stays per request.
        if is_rate_limited(response):
            wait = rate_limit_wait(response)
        else:
            wait = throttle_seconds(response) or 0.0
        next_allowed_at = func.now() + timedelta(seconds=wait)
        statement = insert(RateLimit).values(
            source=SOURCE, tenant=tenant, bucket=bucket, next_allowed_at=next_allowed_at, remaining=remaining
        )
        statement = statement.on_conflict_do_update(
            index_elements=["source", "tenant", "bucket"],
            set_={
                # Never shorten a wait another worker recorded
                "next_allowed_at": func.greatest(RateLimit.next_allowed_at, statement.excluded.next_allowed_at),
                "remaining": statement.excluded.remaining,
            },
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Set DATABASE_URL, e.g. postgresql://ember:...@localhost:5432/ember?sslmode=disable", file=sys.stderr)
        return 2
    concurrency = int(os.environ.get("CONCURRENCY", "4"))
    batch = int(os.environ.get("BATCH", "25"))
    asyncio.run(Worker(database_url, concurrency=concurrency, batch=batch).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
