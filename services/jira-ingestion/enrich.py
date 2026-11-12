"""enrich:jira (EMBER-66): turns Jira rows in raw_events into source_records.

    DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
    SOURCE_CREDENTIALS_API_KEYS=... JIRA_OAUTH_CLIENT_ID=... JIRA_OAUTH_CLIENT_SECRET=... \
    PYTHONPATH=../../packages/source-credentials python3 enrich.py

What it fetches from Jira it fetches with the token Connect Jira stored for the row's site
(EMBER-131, atlassian.py), and only for rows of the tenant that connected the site. A row with no
such token fails and is retried on the next poll: connecting the site is the only way in.

Rows are claimed as docs/ingestion/architecture.md, "Interim: claiming without the job queue",
describes: LISTEN on RAW_EVENTS_CHANNEL (default raw_events), then claim once at start, on any
notification (the payload is ignored), and every POLL_SECONDS. Each row is claimed, enriched and marked in its own transaction, CONCURRENCY
(default 4) at a time. What each event becomes and what is fetched follows
docs/ingestion/jira-webhooks.md. SIGINT or SIGTERM stops claiming, lets in-flight rows finish,
and exits 0.

Reconcile on connect (EMBER-167, jira-webhooks.md "Reconcile on connect"): at start and on every wake,
when none is running, it backfills each connected site with an api credential whose
`reconcile:<site>` cursor isn't `done`, one at a time. RECONCILE_SINCE (an ISO date) limits it to
issues updated since.
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

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from atlassian import OAuthClient, SiteCredentials, api_origin, bearer_headers
from models import Cursor, Installation, RateLimit, RawEvent, SourceCredential, SourceRecord
from pull_test import (
    PullError,
    Response,
    _bucket_total,
    exchange_with_retry,
    get_json,
    get_paged_items,
    is_rate_limited,
    issue_url,
    rate_limit_wait,
    search_issue_keys,
    throttle_seconds,
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
DONE = "done"   # a reconcile cursor's position once its stream has been read to the end

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


def _millis(jira_time: str) -> int:
    return int(datetime.strptime(jira_time, JIRA_TIME).timestamp() * 1000)


def issue_bodies(origin: str, key: str, headers: dict[str, str], exchange) -> list[dict]:
    """One issue for reconcile: the issue with its whole changelog, then its comments, each shaped as the
    webhook body for its latest change (jira:issue_updated, comment_updated), so describe() reads it."""
    issue = get_json(issue_url(origin, key), headers, exchange)
    # As expand=changelog nests it, but every page: GET /issue/{key}/changelog
    issue["changelog"] = {"histories": get_paged_items(origin, f"/rest/api/3/issue/{key}/changelog", "values", headers, exchange)}
    bodies = [{"webhookEvent": "jira:issue_updated", "timestamp": _millis(issue["fields"]["updated"]), "issue": issue}]
    if _bucket_total(issue, "comment") != 0:
        reduced = {"id": issue["id"], "key": issue["key"], "self": issue["self"],
                   "fields": {"summary": issue["fields"].get("summary")}}   # what a comment webhook carries
        bodies += [{"webhookEvent": "comment_updated", "timestamp": _millis(comment["updated"]), "comment": comment,
                    "issue": reduced}
                   for comment in get_paged_items(origin, f"/rest/api/3/issue/{key}/comment", "comments", headers, exchange)]
    return bodies


def reconcile_since(value: str) -> Optional[str]:
    """RECONCILE_SINCE, an ISO date, as a JQL date. Empty = all history."""
    return datetime.fromisoformat(value.strip()[:10]).strftime("%Y-%m-%d") if value.strip() else None


def cursor_upsert(tenant: str, stream: str, position: str):
    statement = insert(Cursor).values(source=SOURCE, tenant=tenant, stream=stream, position=position)
    return statement.on_conflict_do_update(
        index_elements=["source", "tenant", "stream"],
        set_={"position": statement.excluded.position, "updated_at": func.now()},
    )


def make_engine(database_url: str, pool_size: int) -> AsyncEngine:
    """Async engine on asyncpg for a postgres:// or postgresql:// DATABASE_URL, sslmode included."""
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    sslmode = url.query.get("sslmode")
    url = url.difference_update_query(["sslmode"])
    return create_async_engine(url, pool_size=pool_size, connect_args={"ssl": sslmode} if sslmode else {})


class Worker:
    """Claims and enriches Jira raw_events until stop(). Construct it inside the running event loop.

    credentials reads the sites' Connect Jira tokens. exchange is pull_test's
    (method, url, headers, body) -> Response boundary for the API calls. Tests stub both."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        credentials: SiteCredentials,
        exchange=urllib_exchange,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_seconds: float = POLL_SECONDS,
        channel: str = DEFAULT_CHANNEL,
        reconcile_since: Optional[str] = None,
    ) -> None:
        self.engine = engine
        self.session = async_sessionmaker(engine, expire_on_commit=False)
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
        self.reconcile_since = reconcile_since   # a JQL date, e.g. 2026-01-01; None = all history
        self.reconciling: Optional[asyncio.Task] = None
        self.failed_accounts: set[str] = set()   # sites whose reconcile failed, retried after the next poll

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
                        self.failed_accounts.clear()
                    self.wake.clear()
                    if self.reconciling is None or self.reconciling.done():
                        self.reconciling = asyncio.create_task(self.reconcile_pending())
                    await self._drain()
                await asyncio.gather(*self.tasks, *([self.reconciling] if self.reconciling else []))
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
            if row.origin == "reconcile":
                issue_ids = []   # reconcile writes each issue before its comments
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
        """GET one issue through pull_test's get_json, rate limited and retried as _limited_exchange says.
        A 403 or 404 returns None."""
        site = row.account_id
        credential = await self.credentials.fresh(site, tenant=row.tenant) if site else None
        if credential is None:
            raise PullError(f"no Connect Jira token for site {site} and tenant {row.tenant}")
        url = issue_url(api_origin(credential["cloud_id"]), issue_id)
        headers = bearer_headers(credential["access_token"])
        return await asyncio.to_thread(get_json, url, headers, self._limited_exchange(row.tenant, site), optional=True)

    def _limited_exchange(self, tenant: str, site: str):
        """pull_test's exchange_with_retry (429, 5xx) for a thread, with every attempt waiting on and
        updating the rate_limits row for this site."""
        bucket = f"https://{site}"
        loop = asyncio.get_running_loop()

        def limited(method: str, url: str, headers: dict[str, str], body: Optional[bytes]) -> Response:
            asyncio.run_coroutine_threadsafe(self._wait_turn(tenant, bucket), loop).result()
            response = self.exchange(method, url, headers, body)
            asyncio.run_coroutine_threadsafe(self._record_limit(tenant, bucket, response), loop).result()
            return response

        def retrying(method: str, url: str, headers: dict[str, str], body: Optional[bytes]) -> Response:
            return exchange_with_retry(limited, method, url, headers, body)

        return retrying

    async def reconcile_pending(self) -> None:
        """Backfill every connected site whose reconcile hasn't finished (ADR 0010), one at a time.
        A failed one is logged and retried after the next poll."""
        # ponytail: one site and one request at a time. Run them concurrently if a backfill is too slow.
        try:
            async with self.session() as session:
                sites = (await session.execute(
                    select(Installation.account_id, Installation.tenant)
                    .join(SourceCredential, (SourceCredential.source == Installation.source)
                          & (SourceCredential.account_id == Installation.account_id) & (SourceCredential.kind == "api"))
                    .where(Installation.source == SOURCE, Installation.disconnected_at.is_(None),
                           ~exists().where(Cursor.source == SOURCE, Cursor.tenant == Installation.tenant,
                                           Cursor.stream == "reconcile:" + Installation.account_id,
                                           Cursor.position == DONE))
                    .order_by(Installation.added_at)
                )).all()
        except Exception:
            log.exception("reconcile: listing sites failed; retrying on the next wake")
            return
        for site, tenant in sites:
            if self.stopping.is_set():
                return
            if site in self.failed_accounts:
                continue
            try:
                await self.reconcile_site(tenant, site)
            except Exception:
                self.failed_accounts.add(site)
                log.exception("reconcile %s failed; retrying after the next poll", site)

    async def reconcile_site(self, tenant: str, site: str) -> None:
        """Every project in the site's stored JQL, then `reconcile:<site>` = done."""
        credential = await self.credentials.fresh(site, tenant=tenant)
        if credential is None:
            return
        projects = re.findall(r"\d+", credential["jql"])   # project in (<ids>), from Connect Jira
        log.info("reconcile %s (%s): starting, projects %s", site, tenant, ", ".join(projects))
        for project in projects:
            if not await self.reconcile_project(tenant, site, project):
                return
        async with self.engine.begin() as conn:
            await conn.execute(cursor_upsert(tenant, f"reconcile:{site}", DONE))
        log.info("reconcile %s (%s): done", site, tenant)

    async def reconcile_project(self, tenant: str, site: str, project: str) -> bool:
        """The project's issues by key from the stream's cursor (the last key written), each with its changelog
        and comments. The cursor moves with each issue's rows. False when stopping or disconnected."""
        stream = f"reconcile:{site}:{project}"
        async with self.engine.connect() as conn:
            position = await conn.scalar(select(Cursor.position).where(
                Cursor.source == SOURCE, Cursor.tenant == tenant, Cursor.stream == stream))
        if position == DONE:
            return True
        jql = f"project = {project}"
        if position:
            jql += f' AND key > "{position}"'
        if self.reconcile_since:
            jql += f' AND updated >= "{self.reconcile_since}"'
        jql += " ORDER BY key ASC"
        exchange = self._limited_exchange(tenant, site)
        credential = await self.credentials.fresh(site, tenant=tenant)
        if credential is None:
            return False
        # An access token lasts an hour. If it expires mid-search, the 401 fails the site until the next poll,
        # which resumes from the cursor with a fresh one.
        keys = search_issue_keys(api_origin(credential["cloud_id"]), jql,
                                 bearer_headers(credential["access_token"], json_body=True), exchange)
        written = 0
        while (key := await asyncio.to_thread(next, keys, None)) is not None:
            if self.stopping.is_set():
                return False
            credential = await self.credentials.fresh(site, tenant=tenant)
            if credential is None:
                log.info("reconcile %s: disconnected, stopped", site)
                return False
            bodies = await asyncio.to_thread(issue_bodies, api_origin(credential["cloud_id"]), key,
                                             bearer_headers(credential["access_token"]), exchange)
            if not await self.store(tenant, site, bodies, stream, key):
                log.info("reconcile %s: disconnected, stopped", site)
                return False
            written += len(bodies)
            log.info("%s: %s, %d objects so far", stream, key, written)
        async with self.engine.begin() as conn:
            await conn.execute(cursor_upsert(tenant, stream, DONE))
        return True

    async def store(self, tenant: str, site: str, bodies: list[dict], stream: str, position: str) -> bool:
        """Insert the bodies as raw_events (origin reconcile) and move the cursor, in one transaction, only while
        the site is still connected. False when it isn't."""
        received = datetime.now(timezone.utc)
        rows = []
        for body in bodies:
            raw = RawEvent(envelope={"type": SOURCE, "body": body}, received_at=received)
            _kind, object_id, version, _issues = describe(body)
            rows.append({"source": SOURCE, "delivery_id": f"{object_id}@{version}", "account_id": site,
                         "tenant": tenant, "event_name": body["webhookEvent"], "origin": "reconcile",
                         "envelope": raw.envelope, "occurred_at": ordering_value(raw)})
        async with self.engine.begin() as conn:
            # Shared lock: a disconnect waits for this commit, and nothing is written after it
            connected = await conn.scalar(
                select(Installation.account_id)
                .where(Installation.source == SOURCE, Installation.account_id == site,
                       Installation.tenant == tenant, Installation.disconnected_at.is_(None))
                .with_for_update(read=True))
            if connected is None:
                return False
            if rows:
                await conn.execute(insert(RawEvent).values(rows).on_conflict_do_nothing(index_elements=["source", "delivery_id"]))
            await conn.execute(cursor_upsert(tenant, stream, position))
        return True

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
        ring = KeyRing.from_env("api")
        concurrency = int(os.environ.get("CONCURRENCY", "") or DEFAULT_CONCURRENCY)
    except (CredentialError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        since = reconcile_since(os.environ.get("RECONCILE_SINCE", ""))
    except ValueError:
        print("RECONCILE_SINCE must be an ISO date, e.g. 2026-01-01", file=sys.stderr)
        return 2
    client = OAuthClient.from_env()
    if ring is None or client is None:
        print("SOURCE_CREDENTIALS_API_KEYS and the Atlassian app's JIRA_OAUTH_CLIENT_ID and "
              "JIRA_OAUTH_CLIENT_SECRET are required", file=sys.stderr)
        return 2

    async def run() -> None:
        # Each in-flight row holds a connection and opens one at a time for its credential or
        # rate_limits, plus the listener
        engine = make_engine(database_url, pool_size=2 * concurrency + 1)
        try:
            await Worker(
                engine, credentials=SiteCredentials(engine, ring, client, exchange=exchange), exchange=exchange,
                concurrency=concurrency, channel=os.environ.get("RAW_EVENTS_CHANNEL", "").strip() or DEFAULT_CHANNEL,
                reconcile_since=since,
            ).run()
        finally:
            await engine.dispose()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
