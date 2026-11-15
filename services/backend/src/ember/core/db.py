"""Postgres: engines, and LISTEN with reconnect.

Each component makes its own engine from `Database`, sized for its own work, and disposes it when it
stops. One component exhausting its pool, or losing its connections, then never starves another.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import asyncpg
from sqlalchemy import exc
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

LOG = logging.getLogger("ember.db")
DB_TIMEOUT = 5.0  # seconds per connect, pool checkout or statement, unless a component asks for more


def sqlalchemy_url(database_url: str) -> URL:
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
        url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
    return url


def sqlstate(error: BaseException) -> str:
    """Postgres's error code, raised by asyncpg directly (on connect) or wrapped by SQLAlchemy."""
    if isinstance(error, exc.DBAPIError):
        error = error.orig
    return getattr(error, "sqlstate", None) or ""


class Database:
    def __init__(self, database_url: str) -> None:
        self._dsn = database_url  # asyncpg reads libpq's sslmode itself
        self._url = sqlalchemy_url(database_url)

    def engine(self, *, pool_size: int = 2, timeout: float = DB_TIMEOUT,
               statement_timeout: float | None = None) -> AsyncEngine:
        return create_async_engine(
            self._url, pool_size=pool_size, max_overflow=0, pool_pre_ping=True, pool_timeout=timeout,
            hide_parameters=True,  # an error message must not carry a payload into the logs
            connect_args={"timeout": timeout, "command_timeout": statement_timeout or timeout},
        )

    async def connect(self, timeout: float = DB_TIMEOUT) -> asyncpg.Connection:
        """A raw connection outside any pool, for LISTEN."""
        return await asyncpg.connect(self._dsn, timeout=timeout)


class Listener:
    """LISTEN on one channel and call `notified()` on every notification, reconnecting when the connection drops.

    `notified()` is also called after every (re)connect, since a notification sent while the
    connection was down is lost: the worker then claims whatever is waiting. Run it as a task with
    `async with Listener(...)`; leaving the block closes the connection.
    """

    def __init__(self, db: Database, channel: str, notified: Callable[[], None], *,
                 retry_max: float = 30.0) -> None:
        self.db = db
        self.channel = channel
        self.notified = notified
        self.retry_max = retry_max
        self.connected = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Listener:
        self._task = asyncio.create_task(self._run(), name=f"listen:{self.channel}")
        return self

    async def __aexit__(self, *exc_info) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        delay = 1.0
        while True:
            conn = None
            try:
                conn = await self.db.connect()
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _conn: lost.set())
                await conn.add_listener(self.channel, lambda *_args: self.notified())
                self.connected.set()
                delay = 1.0
                self.notified()
                await lost.wait()
                LOG.warning("LISTEN %s: connection lost, reconnecting", self.channel)
            except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as error:
                LOG.warning("LISTEN %s: %s, retrying in %.0fs", self.channel, type(error).__name__, delay)
            finally:
                self.connected.clear()
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        await conn.close(timeout=2)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry_max)
