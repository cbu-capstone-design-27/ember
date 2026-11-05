"""enrich:jira (EMBER-66): turns Jira rows in raw_events into source_records.

    DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
    SOURCE_CREDENTIALS_API_KEYS=... JIRA_OAUTH_CLIENT_ID=... JIRA_OAUTH_CLIENT_SECRET=... \
    JIRA_BASE_URL=https://<site>.atlassian.net JIRA_EMAIL=... JIRA_API_TOKEN=... \
    PYTHONPATH=../../packages/source-credentials python3 enrich.py

What it fetches from Jira it fetches as the site's tenant connected it (EMBER-131): with the token
Connect Jira stored for the site (atlassian.py), when the row's tenant is the one that connected it.
Otherwise it uses the one site in JIRA_BASE_URL with an account's API token, for sites mapped by hand.
Either half may be left out, not both.

Rows are claimed as docs/ingestion/architecture.md, "Interim: claiming without the job queue",
describes: LISTEN on RAW_EVENTS_CHANNEL (default raw_events), then claim once at start, on any
notification (the payload is ignored), and every POLL_SECONDS. Each row is claimed, enriched and marked in its own transaction, CONCURRENCY
(default 4) at a time. What each event becomes and what is fetched follows
docs/ingestion/jira-webhooks.md. SIGINT or SIGTERM stops claiming, lets in-flight rows finish,
and exits 0.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from atlassian import OAuthClient, SiteCredentials, api_origin, bearer_headers
from models import RateLimit, RawEvent, SourceRecord
from pull_test import (
    PullError,
    Response,
    _headers,
    exchange_with_retry,
    get_json,
    is_rate_limited,
    issue_url,
    rate_limit_wait,
    throttle_seconds,
    site_origin,
    urllib_exchange,
)
from source_credentials import CredentialError, KeyRing

SOURCE = "jira"
POLL_SECONDS = 30.0
DEFAULT_CHANNEL = "raw_events"
DEFAULT_CONCURRENCY = 4
JIRA_TIME = "%Y-%m-%dT%H:%M:%S.%f%z"   # 2026-10-13T14:05:11.123-0700

# Webhook body key holding the entity, per source_records kind (jira-webhooks.md, Event types)
PAYLOAD_KEY = {
    "issue": "issue",
    "comment": "comment",
    "worklog": "worklog",
    "attachment": "attachment",
    "issuelink": "issueLink",
    "issue_property": "property",
}
COMMENT_ISSUE = re.compile(r"/issue/(\d+)/comment/")

log = logging.getLogger("enrich:jira")


def kind_of(event: str) -> Optional[str]:
    """source_records kind for a webhookEvent, or None for events that produce no record."""
    if event in ("jira:issue_created", "jira:issue_updated", "jira:issue_deleted"):
        return "issue"
    kind = event.rpartition("_")[0]   # comment_created -> comment, issue_property_set -> issue_property
    return kind if kind in PAYLOAD_KEY else None


def _entity_time(body: dict) -> Optional[str]:
    """The entity's own time: updated for issues, comments and worklogs, created for attachments.
    None for deletes (an entity's updated is its last edit, not the delete), links and properties."""
    event = body.get("webhookEvent", "")
    kind = kind_of(event)
    if kind is None or event.endswith("_deleted"):
        return None
    entity = body.get(PAYLOAD_KEY[kind]) or {}
    if kind == "issue":
        return (entity.get("fields") or {}).get("updated")
    if kind in ("comment", "worklog"):
        return entity.get("updated")
    if kind == "attachment":
        return entity.get("created")
    return None


def ordering_value(raw_event: RawEvent) -> datetime:
    """Jira's time for the event (jira-webhooks.md, Ordering value). Fills source_records.occurred_at."""
    # TODO(EMBER-63): read the raw_events ordering column once it lands
    body = raw_event.envelope["body"]
    value = _entity_time(body)
    if value:
        return datetime.strptime(value, JIRA_TIME)
    return datetime.fromtimestamp(body["timestamp"] / 1000, tz=timezone.utc)


def describe(body: dict) -> Optional[tuple[str, str, str, list[str]]]:
    """(kind, object_id, version, issue ids to fetch when not stored) for one webhook body,
    or None when the event produces no record. Deletes are tombstones: version deleted:<timestamp>."""
    event = body.get("webhookEvent", "")
    kind = kind_of(event)
    if kind is None:
        return None
    entity = body[PAYLOAD_KEY[kind]]
    issue_ids: list[str] = []
    if kind == "issue":
        object_id = str(entity["id"])
    elif kind == "comment":
        issue_ids = [COMMENT_ISSUE.search(entity["self"]).group(1)]
        object_id = f"{issue_ids[0]}/comment/{entity['id']}"
    elif kind == "worklog":
        issue_ids = [str(entity["issueId"])]
        object_id = f"{issue_ids[0]}/worklog/{entity['id']}"
    elif kind == "issue_property":
        object_id = f"{body['issue']['id']}/property/{entity['key']}"
    else:   # attachment, issuelink
        if kind == "issuelink":
            issue_ids = [str(entity["sourceIssueId"]), str(entity["destinationIssueId"])]
        object_id = f"{kind}/{entity['id']}"
    if event.endswith("_deleted"):
        return kind, object_id, f"deleted:{body['timestamp']}", []
    # A body without the entity's time (a hand-written fixture, a link, a property) is versioned by when it happened
    return kind, object_id, _entity_time(body) or str(body["timestamp"]), issue_ids


def make_engine(database_url: str, pool_size: int) -> AsyncEngine:
    """Async engine on asyncpg for a postgres:// or postgresql:// DATABASE_URL, sslmode included."""
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    sslmode = url.query.get("sslmode")
    url = url.difference_update_query(["sslmode"])
    return create_async_engine(url, pool_size=pool_size, connect_args={"ssl": sslmode} if sslmode else {})


class Worker:
    """Claims and enriches Jira raw_events until stop(). Construct it inside the running event loop.

    exchange is pull_test's (method, url, headers, body) -> Response boundary. Tests stub it.
    credentials reads Connect Jira's tokens; origin, email and api_token are the one site read with an
    API token. Give either or both."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        origin: Optional[str] = None,
        email: str = "",
        api_token: str = "",
        credentials: Optional[SiteCredentials] = None,
        exchange=urllib_exchange,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_seconds: float = POLL_SECONDS,
        channel: str = DEFAULT_CHANNEL,
    ) -> None:
        self.engine = engine
        self.session = async_sessionmaker(engine, expire_on_commit=False)
        self.origin = origin
        self.email = email
        self.api_token = api_token
        self.credentials = credentials
        self.exchange = exchange
        self.poll_seconds = poll_seconds
        self.channel = channel
        self.slots = asyncio.Semaphore(concurrency)
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        # ponytail: a failing row is skipped until the next poll and then retried, forever. The job
        # queue's dead-letter replaces this.
        self.failed: set[int] = set()

    def stop(self) -> None:
        if not self.stopping.is_set():
            log.info("stopping: finishing %d in-flight row(s)", len(self.tasks))
        self.stopping.set()
        self.wake.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        try:
            async with self.engine.connect() as listen:
                listener = (await listen.get_raw_connection()).driver_connection
                await listener.add_listener(self.channel, self._notified)
                log.info("listening on %s", self.channel)
                self.wake.set()   # catch up on rows inserted while stopped
                while not self.stopping.is_set():
                    try:
                        await asyncio.wait_for(self.wake.wait(), self.poll_seconds)
                    except asyncio.TimeoutError:
                        self.failed.clear()
                    self.wake.clear()
                    await self._drain()
                await asyncio.gather(*list(self.tasks))
                await listener.remove_listener(self.channel, self._notified)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
        log.info("stopped")

    def _notified(self, _conn, _pid, _channel, _payload: str) -> None:
        # Any notification wakes the claim, which filters by source. The payload isn't part of the contract.
        self.wake.set()

    async def _drain(self) -> None:
        """Start one task per claimable row, up to CONCURRENCY at once, until none is left."""
        while not self.stopping.is_set():
            await self.slots.acquire()
            if self.stopping.is_set():
                self.slots.release()
                return
            claimed = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._process_next(claimed))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            if not await claimed:
                return

    async def _process_next(self, claimed: asyncio.Future) -> None:
        """Claim one row, enrich it and set enriched_at, in one transaction. claimed gets whether a row was taken."""
        row_id = None
        try:
            async with self.session() as session, session.begin():
                row = await session.scalar(
                    select(RawEvent)
                    .where(
                        RawEvent.source == SOURCE,
                        RawEvent.enriched_at.is_(None),
                        RawEvent.tenant.is_not(None),
                        RawEvent.id.not_in(list(self.failed)),
                    )
                    .order_by(RawEvent.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                claimed.set_result(row is not None)
                if row is None:
                    return
                row_id = row.id
                await self._enrich(session, row)
            log.info("enriched raw_events %d (%s)", row_id, row.event_name)
        except Exception:
            if row_id is not None:
                self.failed.add(row_id)
            log.exception("raw_events %s failed; retrying on the next poll", row_id)
        finally:
            if not claimed.done():
                claimed.set_result(False)
            self.slots.release()

    async def _enrich(self, session, row: RawEvent) -> None:
        body = row.envelope["body"]
        described = describe(body)
        if described is not None:
            kind, object_id, version, issue_ids = described
            record: dict = {"payload": body}
            if version.startswith("deleted:"):
                record["deleted"] = True
            issues = {}
            for issue_id in issue_ids:
                if not await self._issue_stored(session, row.tenant, issue_id):
                    issue = await self._fetch(row, issue_id)
                    if issue is not None:
                        issues[issue_id] = issue
            if issues:
                record["issues"] = issues
            await session.execute(
                insert(SourceRecord)
                .values(
                    source=SOURCE, tenant=row.tenant, kind=kind, object_id=object_id, version=version,
                    occurred_at=ordering_value(row), record=record, raw_event_id=row.id,
                )
                .on_conflict_do_nothing(index_elements=["source", "tenant", "object_id", "version"])
            )
        # clock_timestamp(), not now(): now() is when this transaction claimed the row, before any fetch
        await session.execute(update(RawEvent).where(RawEvent.id == row.id).values(enriched_at=func.clock_timestamp()))

    async def _issue_stored(self, session, tenant: str, issue_id: str) -> bool:
        found = await session.scalar(
            select(SourceRecord.id)
            .where(SourceRecord.source == SOURCE, SourceRecord.tenant == tenant,
                   SourceRecord.kind == "issue", SourceRecord.object_id == issue_id)
            .limit(1)
        )
        return found is not None

    async def _fetch(self, row: RawEvent, issue_id: str) -> Optional[dict]:
        """GET one issue through pull_test's get_json and exchange_with_retry (429, 5xx), with every
        attempt waiting on and updating the rate_limits row for this site. A 403 or 404 returns None."""
        site = row.account_id
        credential = None
        if self.credentials is not None and site:
            credential = await self.credentials.fresh(site, tenant=row.tenant)
        if credential is not None:
            url = issue_url(api_origin(credential["cloud_id"]), issue_id)
            headers = bearer_headers(credential["access_token"])
        elif self.origin and (not site or site == urlsplit(self.origin).hostname):
            url = issue_url(self.origin, issue_id)
            headers = _headers(self.email, self.api_token)
        else:
            raise PullError(f"no credentials for site {site}")
        bucket = f"https://{site}" if site else self.origin   # one bucket per site, whichever way it's called
        loop = asyncio.get_running_loop()

        def limited(method: str, url: str, headers: dict[str, str], body: Optional[bytes]) -> Response:
            asyncio.run_coroutine_threadsafe(self._wait_turn(row.tenant, bucket), loop).result()
            response = self.exchange(method, url, headers, body)
            asyncio.run_coroutine_threadsafe(self._record_limit(row.tenant, bucket, response), loop).result()
            return response

        def retrying(method: str, url: str, headers: dict[str, str], body: Optional[bytes]) -> Response:
            return exchange_with_retry(limited, method, url, headers, body)

        return await asyncio.to_thread(get_json, url, headers, retrying, optional=True)

    @staticmethod
    def _limit_key(tenant: str, bucket: str):
        return (RateLimit.source == SOURCE, RateLimit.tenant == tenant, RateLimit.bucket == bucket)

    async def _wait_turn(self, tenant: str, bucket: str) -> None:
        """Sleep until the site's next_allowed_at, by the database's clock."""
        async with self.engine.connect() as conn:
            delay = await conn.scalar(
                select(func.extract("epoch", RateLimit.next_allowed_at - func.now())).where(*self._limit_key(tenant, bucket))
            )
        if delay is not None and delay > 0:
            log.info("rate limit: waiting %.1fs for %s", delay, bucket)
            await asyncio.sleep(float(delay))

    async def _record_limit(self, tenant: str, bucket: str, response: Response) -> None:
        """Push next_allowed_at out by pull_test's wait for a 429 or a low quota, and keep X-RateLimit-Remaining."""
        now = time.time()
        wait = max(rate_limit_wait(response, now=now) if is_rate_limited(response) else 0.0,
                   throttle_seconds(response, now=now) or 0.0)
        headers = {key.lower(): value for key, value in response.headers.items()}
        remaining = headers.get("x-ratelimit-remaining")
        stmt = insert(RateLimit).values(
            source=SOURCE, tenant=tenant, bucket=bucket,
            next_allowed_at=func.now() + timedelta(seconds=wait),
            remaining=int(remaining) if remaining and remaining.isdigit() else None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "tenant", "bucket"],
            set_={
                "next_allowed_at": func.greatest(RateLimit.next_allowed_at, stmt.excluded.next_allowed_at),
                "remaining": func.coalesce(stmt.excluded.remaining, RateLimit.remaining),
            },
        )
        async with self.engine.begin() as conn:
            await conn.execute(stmt)


def main(exchange=urllib_exchange) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        base_url = os.environ.get("JIRA_BASE_URL", "").strip()
        origin = site_origin(base_url) if base_url else None
        ring = KeyRing.from_env("api")
        client = OAuthClient.from_env()
        concurrency = int(os.environ.get("CONCURRENCY", "") or DEFAULT_CONCURRENCY)
    except (PullError, CredentialError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if (ring is None) != (client is None):
        print("Connect Jira's tokens need both SOURCE_CREDENTIALS_API_KEYS and the JIRA_OAUTH_CLIENT_ID and "
              "JIRA_OAUTH_CLIENT_SECRET of the Atlassian app, or neither", file=sys.stderr)
        return 2
    if origin is None and ring is None:
        print("Give JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN, or SOURCE_CREDENTIALS_API_KEYS and the "
              "Atlassian app's JIRA_OAUTH_CLIENT_ID and JIRA_OAUTH_CLIENT_SECRET, or both", file=sys.stderr)
        return 2

    async def run() -> None:
        # Each in-flight row holds a connection and opens one at a time for its credential or
        # rate_limits, plus the listener
        engine = make_engine(database_url, pool_size=2 * concurrency + 1)
        credentials = SiteCredentials(engine, ring, client, exchange=exchange) if ring else None
        log.info("Jira credentials: %s", ", ".join(
            (["Connect Jira tokens"] if credentials else []) + ([f"API token for {origin}"] if origin else [])))
        try:
            await Worker(
                engine, origin=origin, email=os.environ.get("JIRA_EMAIL", ""),
                api_token=os.environ.get("JIRA_API_TOKEN", ""), credentials=credentials, exchange=exchange,
                concurrency=concurrency, channel=os.environ.get("RAW_EVENTS_CHANNEL", "").strip() or DEFAULT_CHANNEL,
            ).run()
        finally:
            await engine.dispose()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
