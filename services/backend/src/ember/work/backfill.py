"""The base of every backfill: reads each connected account's history into raw_events (ADR 0010).

A subclass says what to read (`account`); this does the rest, the same for every source:

- **When.** Every `check_seconds` it looks for connected accounts that are due: never read, or last read
  more than `every` ago (cursor `<account_stream>:<account_id>`). A new connection is read within a check.
- **Rows.** `store` inserts rows with origin `reconcile`, shaped as the webhook delivery would be, so the
  source's enrich reads them unchanged. The unique delivery_id makes re-reading anything a no-op.
- **Progress.** The subclass keeps finer cursors (`cursor`, `save_cursor`); a stop or crash re-reads at
  most what was in hand.
- **Limits.** Calls wait on the shared `rate_limits` rows; a rate limit is slept out (`patiently`), since a
  backfill holds no claim. The source refusing the token disconnects the account, unless an owner
  reconnected it since. One replica at a time (an advisory lock).
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, TypeVar

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ember.core import http
from ember.core.credentials import Credentials, disconnect, live
from ember.core.db import Database
from ember.core.errors import ConfigError, Throttled
from ember.core.models import Cursor, RawEvent
from ember.runtime.component import Component
from ember.work.queue_worker import wait_any
from ember.work.rate_limits import SharedRateLimit
from source_credentials import ENV

LOG = logging.getLogger("ember.backfill")
T = TypeVar("T")
INSERT_CHUNK = 1000


class Stopped(Exception):
    """Shutdown started mid-backfill; run() ends on it."""


class Backfill(Component):
    source: ClassVar[str]
    limits_rule: ClassVar[http.Limits]
    refused: ClassVar[type[Exception]]  # the source refused the account's token
    account_stream: ClassVar[str]       # the cursor of a full pass is "<account_stream>:<account_id>"

    def __init__(self, db: Database, credentials: Credentials, *, exchange: http.Exchange = http.urllib_exchange,
                 every: timedelta = timedelta(hours=1), check_seconds: float = 30.0) -> None:
        self.name = f"backfill:{self.source}"
        self.db = db
        self.credentials = credentials
        self.exchange = exchange
        self.every = every
        self.check_seconds = check_seconds
        self.engine: AsyncEngine | None = None
        self.sessions: async_sessionmaker | None = None
        self.limits = SharedRateLimit(self.source, self.limits_rule, lambda: self.sessions, wait_in_job=float("inf"))
        self.current: str | None = None
        self.last: dict[str, Any] | None = None

    @abstractmethod
    async def account(self, account_id: str, tenant: str, secret: dict, stopping: asyncio.Event) -> dict[str, Any]:
        """Read one account since its cursors and store what is new. A summary for /status, with "rows"."""

    async def start(self) -> None:
        if self.credentials.ring("api") is None:  # every connected account's token is sealed with it
            raise ConfigError(f"{ENV['api']} is not set")
        self.engine = self.db.engine(pool_size=3)  # the lock, the writes, the rate_limits rows
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        if self.engine is not None:
            engine, self.engine, self.sessions = self.engine, None, None
            await engine.dispose()

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.run_once(stopping)
            except Stopped:
                break
            await wait_any(stopping, timeout=self.check_seconds)

    def lock(self, unlock: bool = False):
        return text(f"select pg_{'advisory_unlock' if unlock else 'try_advisory_lock'}(hashtext('{self.name}'))")

    async def due(self) -> list[tuple[str, str]]:
        """(account_id, tenant) of every connected account not read within `every`."""
        assert self.engine is not None
        accounts = live(self.source).subquery()
        last = (select(Cursor.updated_at)
                .where(Cursor.source == self.source, Cursor.tenant == accounts.c.tenant,
                       Cursor.stream == f"{self.account_stream}:" + accounts.c.account_id)
                .scalar_subquery())
        query = (select(accounts.c.account_id, accounts.c.tenant)
                 .where(func.coalesce(last, datetime(1970, 1, 1, tzinfo=timezone.utc)) < func.now() - self.every)
                 .order_by(accounts.c.account_id))
        async with self.engine.connect() as conn:
            return [(row.account_id, row.tenant) for row in await conn.execute(query)]

    async def run_once(self, stopping: asyncio.Event) -> bool:
        """Backfill every due account, unless another replica is. True when this one ran."""
        assert self.engine is not None
        async with self.engine.connect() as lock:
            if not await lock.scalar(self.lock()):
                return False
            try:
                for account_id, tenant in await self.due():
                    self.current = account_id
                    try:
                        await self.pass_over(account_id, tenant, stopping)
                    except (Stopped, asyncio.CancelledError):
                        raise
                    except Exception as error:  # the next check tries again
                        LOG.error("%s of %s failed: %s: %s", self.name, account_id, type(error).__name__, error)
            finally:
                self.current = None
                await lock.scalar(self.lock(unlock=True))
        return True

    async def now(self) -> datetime:
        """The database's clock, which cursors and rate limits are kept by."""
        async with self.sessions() as session:
            return await session.scalar(select(func.clock_timestamp()))

    async def pass_over(self, account_id: str, tenant: str, stopping: asyncio.Event) -> None:
        read_at = await self.now()
        secret = await self.credentials.open(self.source, account_id, "api", tenant=tenant)
        if not secret:
            return  # disconnected since due() read it
        try:
            summary = await self.account(account_id, tenant, secret, stopping)
        except self.refused as error:
            async with self.sessions.begin() as session:
                if await disconnect(session, self.source, account_id, stored_before=read_at):
                    LOG.error("%s; disconnected %s account %s", error, self.source, account_id)
            return
        await self.save_cursor(tenant, f"{self.account_stream}:{account_id}", read_at.isoformat())
        self.last = {self.account_stream: account_id, **summary, "at": read_at.isoformat()}
        LOG.info("%s read %s: %s", self.name, account_id, summary)

    async def patiently(self, stopping: asyncio.Event, call: Callable[[], Awaitable[T]], what: str) -> T:
        """One API call, waiting out its rate limits; Stopped once shutdown starts."""
        while True:
            if stopping.is_set():
                raise Stopped()
            try:
                return await call()
            except Throttled as limit:
                LOG.info("%s %s is rate limited; waiting %.0fs", self.name, what, limit.retry_after)
                await wait_any(stopping, timeout=limit.retry_after)

    async def cursor(self, tenant: str, stream: str) -> str | None:
        async with self.sessions() as session:
            return await session.scalar(select(Cursor.position).where(
                Cursor.source == self.source, Cursor.tenant == tenant, Cursor.stream == stream))

    async def save_cursor(self, tenant: str, stream: str, position: str) -> None:
        statement = insert(Cursor).values(source=self.source, tenant=tenant, stream=stream, position=position)
        async with self.sessions.begin() as session:
            await session.execute(statement.on_conflict_do_update(
                index_elements=["source", "tenant", "stream"],
                set_={"position": statement.excluded.position, "updated_at": func.now()}))

    async def store(self, account_id: str, tenant: str, rows: list[tuple[str, str, datetime, dict]]) -> int:
        """Insert (delivery_id, event_name, occurred_at, body) rows; a delivery_id already stored is skipped.
        Returns the new rows."""
        if not rows:
            return 0
        values = [{"source": self.source, "delivery_id": delivery_id, "account_id": account_id, "tenant": tenant,
                   "event_name": name, "origin": "reconcile", "occurred_at": occurred,
                   "envelope": {"type": self.source, "body": body}}
                  for delivery_id, name, occurred, body in rows]
        stored = 0
        async with self.sessions.begin() as session:
            for start in range(0, len(values), INSERT_CHUNK):  # Postgres takes at most 32767 parameters
                result = await session.execute(insert(RawEvent).values(values[start : start + INSERT_CHUNK])
                                               .on_conflict_do_nothing(index_elements=["source", "delivery_id"])
                                               .returning(RawEvent.id))
                stored += len(result.all())
        return stored

    def status(self) -> dict[str, Any]:
        return {"running": self.current, "last": self.last}
