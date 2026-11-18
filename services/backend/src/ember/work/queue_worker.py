"""The base of every worker that claims rows from a Postgres table: enrich today, extract and load later.

docs/ingestion/architecture.md, "Workers: start, wake and stop" and "Concurrency", is the design:

- **Wake.** It LISTENs on the table's channel, so the commit that inserts a row wakes it in under a
  second. It also claims once at start and after every reconnect, which catches up on rows inserted
  while it was stopped, and every `poll_seconds`, which catches retries that have come due.
- **Claim.** `select ... for update skip locked`, in the order `pending()` gives (the source's
  ordering value, EMBER-63), so replicas and concurrent jobs never claim the same row.
- **Concurrency.** Up to `concurrency` jobs at once, each its own transaction over up to `batch` rows,
  with a savepoint per row so one bad row fails alone.
- **Failures.** A row that fails stays unclaimed and is skipped for a while: until `retry_after`
  for Throttled, with backoff for Transient (and any unclassified error), and for an hour for
  Permanent. The skip list lives in memory, so a restart retries everything once.
- **Stop.** Once `stopping` is set it claims nothing more, finishes the jobs in hand, and returns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ember.core.db import Database, Listener
from ember.core.errors import Permanent, Throttled
from ember.runtime.component import Component

LOG = logging.getLogger("ember.work")
RETRY_MAX = 300.0          # seconds; the longest backoff for a transient failure
PERMANENT_SKIP = 3600.0    # seconds a permanently failed row is left alone

Row = TypeVar("Row")


async def wait_any(*events: asyncio.Event, timeout: float) -> None:
    """Return once any event is set, or after `timeout` seconds."""
    waits = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            task.cancel()


class QueueWorker(Component, Generic[Row]):
    channel: ClassVar[str]  # the NOTIFY channel the table's insert trigger uses
    model: ClassVar[type]   # the mapped table; rows need an integer `id`
    # Connections a job holds at once: its claim's transaction, plus any it opens on the side (a
    # rate-limit row updated outside that transaction)
    connections_per_job: ClassVar[int] = 1

    def __init__(self, db: Database, *, name: str, concurrency: int = 4, batch: int = 1,
                 poll_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        if concurrency < 1 or batch < 1:
            raise ValueError("concurrency and batch must be at least 1")
        self.db = db
        self.name = name
        self.concurrency = concurrency
        self.batch = batch
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.engine: AsyncEngine | None = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None
        self.wake = asyncio.Event()
        self.processed = 0
        self._retry_at: dict[int, float] = {}
        self._attempts: dict[int, int] = {}

    @abstractmethod
    def pending(self) -> Select:
        """The rows waiting for this worker, ordered by the source's ordering value then id.

        The base adds the skip list, the limit and `for update skip locked`.
        """

    @abstractmethod
    async def process(self, session: AsyncSession, row: Row) -> None:
        """Do the work for one row, in the claim's transaction (inside its own savepoint).

        Write the output and mark the row done here, so both commit together. Raise Throttled,
        Transient or Permanent to say how to retry; anything else counts as Transient.
        """

    async def start(self) -> None:
        self.engine = self.db.engine(pool_size=self.concurrency * self.connections_per_job + 1)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        if self.engine is not None:
            engine, self.engine, self.sessions = self.engine, None, None
            await engine.dispose()

    async def run(self, stopping: asyncio.Event) -> None:
        async with Listener(self.db, self.channel, self.wake.set):
            while not stopping.is_set():
                self.wake.clear()
                await self.drain(stopping)
                await wait_any(self.wake, stopping, timeout=self.poll_seconds)

    async def drain(self, stopping: asyncio.Event) -> None:
        """Run jobs, up to `concurrency` at once, until a claim finds nothing or shutdown starts."""
        async def jobs() -> None:
            while not stopping.is_set() and await self.job():
                pass
        # A TaskGroup, not gather: when one job fails, gather would leave the others running on into
        # the restart, past `concurrency` and the pool's size
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(self.concurrency):
                    group.create_task(jobs())
        except ExceptionGroup as errors:  # the first failure, so /status says why, not "ExceptionGroup"
            raise errors.exceptions[0] from None

    def status(self) -> dict[str, Any]:
        return {"processed": self.processed, "retrying": len(self._retry_at)}

    def skipped(self) -> list[int]:
        now = self.clock()
        for row_id in [row_id for row_id, at in self._retry_at.items() if at <= now]:
            del self._retry_at[row_id]
        return list(self._retry_at)

    def claim(self) -> Select:
        query = self.pending()
        skip = self.skipped()
        if skip:
            query = query.where(self.model.id.not_in(skip))
        return query.limit(self.batch).with_for_update(skip_locked=True)

    async def job(self) -> bool:
        """Claim up to `batch` rows and process them in one transaction. False when nothing was waiting."""
        assert self.sessions is not None
        async with self.sessions() as session, session.begin():
            rows = (await session.scalars(self.claim())).all()
            for row in rows:
                try:
                    async with session.begin_nested():
                        await self.process(session, row)
                except Exception as error:
                    self.failed(row, error)
                else:
                    self._attempts.pop(row.id, None)
                    self.processed += 1
        return bool(rows)

    def failed(self, row: Any, error: Exception) -> None:
        if isinstance(error, Throttled):
            delay, kind = error.retry_after, "throttled"
        elif isinstance(error, Permanent):
            delay, kind = PERMANENT_SKIP, "permanent"
        else:
            attempts = self._attempts[row.id] = self._attempts.get(row.id, 0) + 1
            delay, kind = min(2.0 ** attempts, RETRY_MAX), "transient"
        self._retry_at[row.id] = self.clock() + delay
        log = LOG.error if kind == "permanent" else LOG.warning
        log("%s row %s %s, retrying in %.0fs: %s: %s", self.name, row.id, kind, delay, type(error).__name__, error)
