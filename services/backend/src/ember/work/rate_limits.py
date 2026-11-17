"""The shared `rate_limits` row a source's API calls wait on and update (architecture.md, Storage).

One row per (source, tenant, bucket): a GitHub installation, a Jira site. Every call by every worker
and replica waits until the row's next_allowed_at, by the database's clock, then pushes it out by
what the response asked for. A wait longer than `wait_in_job` raises Throttled, so the row is set
aside instead of holding its claim's transaction that long.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ember.core import http
from ember.core.errors import Throttled
from ember.core.models import RateLimit

WAIT_IN_JOB = 30.0


class SharedRateLimit:
    """`sessions` returns the worker's session factory, which exists only while it runs."""

    def __init__(self, source: str, limits: http.Limits,
                 sessions: Callable[[], async_sessionmaker[AsyncSession] | None], *,
                 wait_in_job: float = WAIT_IN_JOB) -> None:
        self.source = source
        self.limits = limits
        self.sessions = sessions
        self.wait_in_job = wait_in_job

    def exchange(self, tenant: str, bucket: str, inner: http.Exchange) -> http.Exchange:
        """A blocking exchange, for a thread, that waits on and updates the bucket's row around `inner`."""
        loop = asyncio.get_running_loop()

        def exchange(method: str, url: str, headers: dict[str, str], body: bytes | None) -> http.Response:
            asyncio.run_coroutine_threadsafe(self.wait_turn(tenant, bucket), loop).result()
            response = inner(method, url, headers, body)
            asyncio.run_coroutine_threadsafe(self.record(tenant, bucket, response), loop).result()
            return response

        return exchange

    def _session(self) -> async_sessionmaker[AsyncSession]:
        sessions = self.sessions()
        assert sessions is not None, "the worker is not running"
        return sessions

    def _row(self, tenant: str, bucket: str) -> tuple:
        return (RateLimit.source == self.source, RateLimit.tenant == tenant, RateLimit.bucket == bucket)

    async def wait_turn(self, tenant: str, bucket: str) -> None:
        async with self._session()() as session:
            delay = await session.scalar(
                select(func.extract("epoch", RateLimit.next_allowed_at - func.now())).where(*self._row(tenant, bucket)))
        if delay is None or delay <= 0:
            return
        if delay > self.wait_in_job:
            raise Throttled(f"{self.source} {bucket} is rate limited", retry_after=float(delay))
        await asyncio.sleep(float(delay))

    async def record(self, tenant: str, bucket: str, response: http.Response) -> None:
        left = http.remaining(response)
        statement = insert(RateLimit).values(
            source=self.source, tenant=tenant, bucket=bucket,
            next_allowed_at=func.now() + timedelta(seconds=self.limits.pause(response)),
            remaining=None if left is None else int(left),
        )
        statement = statement.on_conflict_do_update(
            index_elements=["source", "tenant", "bucket"],
            set_={
                # Never shorten a wait another worker recorded
                "next_allowed_at": func.greatest(RateLimit.next_allowed_at, statement.excluded.next_allowed_at),
                "remaining": func.coalesce(statement.excluded.remaining, RateLimit.remaining),
            },
        )
        async with self._session().begin() as session:
            await session.execute(statement)
