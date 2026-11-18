"""backfill:slack: reads each connected workspace's channel history into raw_events (ADR 0010, slack-webhooks.md
"Known gaps and catch-up"). Replaces services/slack-ingestion/pull_test.py.

- **When.** Every `check_seconds` it looks for connected workspaces that are due: never read, or last read more
  than `every` ago. So a new connection is backfilled within a check, and every workspace is caught up hourly.
- **What.** Every channel the bot is a member of (`users.conversations`): `conversations.history` since the
  channel's cursor, and `conversations.replies` for each thread in it.
- **Rows.** Each message becomes a `raw_events` row shaped as the webhook body would be, with origin `reconcile`
  and delivery_id `<channel>/<ts>@<version>` (slack-webhooks.md, delivery_id). An edited message is shaped as the
  `message_changed` delivery Slack sends for it. enrich:slack reads them unchanged, and a webhook copy and a
  backfill copy of one version fold into one source_records row.
- **Progress.** Cursor `channel:<id>` holds the newest ts read, written when the channel is done; cursor
  `workspace:<team>` the time of the last full pass. A stop or crash re-reads at most the channel in hand, and
  the unique delivery_id makes that a no-op.
- **Limits.** Calls wait on the shared `rate_limits` rows, and a rate limit is slept out here: history and replies
  allow one call a minute for a non-Marketplace app. One replica at a time (an advisory lock).

Not covered yet: a reply posted since the last pass to a thread older than the channel's cursor (history only
returns newer parents). The live reply event covers it unless its delivery was lost.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ember.connectors.slack.enrich import LIMITS, SOURCE, TokenRefused, api
from ember.connectors.slack.webhook import slack_time
from ember.core import http
from ember.core.credentials import Credentials, disconnect, live
from ember.core.db import Database
from ember.core.errors import ConfigError, Throttled
from ember.core.models import Cursor, RawEvent
from ember.runtime.component import Component
from ember.work.queue_worker import wait_any
from ember.work.rate_limits import SharedRateLimit
from source_credentials import ENV

LOG = logging.getLogger("ember.slack.backfill")
LOCK = "select pg_try_advisory_lock(hashtext('backfill:slack'))"
UNLOCK = "select pg_advisory_unlock(hashtext('backfill:slack'))"
PAGE = 200  # Slack caps a non-Marketplace app's history and replies at 15 a page whatever is asked


class Stopped(Exception):
    pass


def as_delivery(team: str, channel: str, message: dict) -> tuple[str, str, datetime, dict]:
    """(delivery_id, event_name, occurred_at, body) for one message read from history or replies."""
    ts = message["ts"]
    edited = (message.get("edited") or {}).get("ts")
    if edited:  # as Slack delivers an edit: when it was edited, carrying the message as it is now
        event = {"type": "message", "subtype": "message_changed", "channel": channel, "ts": edited, "hidden": True,
                 "message": message}
    else:
        event = {**message, "type": "message", "channel": channel}
    name = "message" + (f":{event['subtype']}" if event.get("subtype") else "")
    body = {"type": "event_callback", "team_id": team, "event": event}
    return f"{channel}/{ts}@{edited or ts}", name, slack_time(edited or ts), body


class SlackBackfill(Component):
    name = "backfill:slack"

    def __init__(self, db: Database, credentials: Credentials, *, exchange: http.Exchange = http.urllib_exchange,
                 every: timedelta = timedelta(hours=1), check_seconds: float = 30.0) -> None:
        self.db = db
        self.credentials = credentials
        self.exchange = exchange
        self.every = every
        self.check_seconds = check_seconds
        self.engine: AsyncEngine | None = None
        self.limits = SharedRateLimit(SOURCE, LIMITS, lambda: self._sessions, wait_in_job=float("inf"))
        self._sessions = None
        self.current: str | None = None
        self.last: dict[str, Any] | None = None

    async def start(self) -> None:
        if self.credentials.ring("api") is None:
            raise ConfigError(f"{ENV['api']} is not set")
        self.engine = self.db.engine(pool_size=3)  # the lock, the writes, the rate_limits rows
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        if self.engine is not None:
            engine, self.engine, self._sessions = self.engine, None, None
            await engine.dispose()

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.run_once(stopping)
            except Stopped:
                break
            await wait_any(stopping, timeout=self.check_seconds)

    async def due(self) -> list[tuple[str, str]]:
        """(team, tenant) of every connected workspace not read within `every`."""
        assert self.engine is not None
        workspaces = live(SOURCE).subquery()
        last = (select(Cursor.updated_at)
                .where(Cursor.source == SOURCE, Cursor.tenant == workspaces.c.tenant,
                       Cursor.stream == "workspace:" + workspaces.c.account_id)
                .scalar_subquery())
        query = (select(workspaces.c.account_id, workspaces.c.tenant)
                 .where(func.coalesce(last, datetime(1970, 1, 1, tzinfo=timezone.utc)) < func.now() - self.every)
                 .order_by(workspaces.c.account_id))
        async with self.engine.connect() as conn:
            return [(row.account_id, row.tenant) for row in await conn.execute(query)]

    async def run_once(self, stopping: asyncio.Event) -> bool:
        """Backfill every due workspace, unless another replica is. True when this one ran."""
        assert self.engine is not None
        async with self.engine.connect() as lock:
            if not await lock.scalar(text(LOCK)):
                return False
            try:
                for team, tenant in await self.due():
                    self.current = team
                    try:
                        await self.workspace(team, tenant, stopping)
                    except (Stopped, asyncio.CancelledError):
                        raise
                    except Exception as error:  # the next check tries again
                        LOG.error("backfill of Slack workspace %s failed: %s: %s", team, type(error).__name__, error)
            finally:
                self.current = None
                await lock.scalar(text(UNLOCK))
        return True

    async def workspace(self, team: str, tenant: str, stopping: asyncio.Event) -> None:
        async with self._sessions() as session:
            read_at = await session.scalar(select(func.clock_timestamp()))
        secret = await self.credentials.open(SOURCE, team, "api", tenant=tenant)
        token = (secret or {}).get("bot_token")
        if not token:
            return  # disconnected since due() read it
        call = self._caller(team, tenant, token, stopping)
        rows = channels = 0
        try:
            cursor = ""
            while True:
                page = await call("users.conversations", {"types": "public_channel,private_channel",
                                                          "exclude_archived": "true", "limit": PAGE, "cursor": cursor})
                for channel in (page or {}).get("channels") or []:
                    rows += await self.channel(team, tenant, channel["id"], call)
                    channels += 1
                cursor = ((page or {}).get("response_metadata") or {}).get("next_cursor") or ""
                if not cursor:
                    break
        except TokenRefused as error:
            async with self._sessions.begin() as session:
                if await disconnect(session, SOURCE, team, stored_before=read_at):
                    LOG.error("%s; disconnected Slack workspace %s", error, team)
            return
        await self.save_cursor(tenant, f"workspace:{team}", read_at.isoformat())
        self.last = {"workspace": team, "channels": channels, "rows": rows, "at": read_at.isoformat()}
        LOG.info("backfilled Slack workspace %s: %d channel(s), %d new row(s)", team, channels, rows)

    async def channel(self, team: str, tenant: str, channel_id: str, call) -> int:
        """Store the channel's messages since its cursor, and every thread among them. New rows."""
        stream = f"channel:{channel_id}"
        async with self._sessions() as session:
            oldest = await session.scalar(select(Cursor.position).where(
                Cursor.source == SOURCE, Cursor.tenant == tenant, Cursor.stream == stream))
        newest, rows, cursor = oldest or "0", 0, ""
        while True:
            page = await call("conversations.history", {"channel": channel_id, "oldest": oldest or "0",
                                                        "limit": PAGE, "cursor": cursor})
            messages = (page or {}).get("messages") or []
            rows += await self.store(team, tenant, channel_id, messages)
            for message in messages:
                if message.get("reply_count") and message.get("thread_ts") == message.get("ts"):
                    rows += await self.thread(team, tenant, channel_id, message["ts"], call)
                if float(message["ts"]) > float(newest):
                    newest = message["ts"]
            cursor = ((page or {}).get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        await self.save_cursor(tenant, stream, newest)
        return rows

    async def thread(self, team: str, tenant: str, channel_id: str, thread_ts: str, call) -> int:
        rows, cursor = 0, ""
        while True:
            page = await call("conversations.replies", {"channel": channel_id, "ts": thread_ts, "limit": PAGE,
                                                        "cursor": cursor})
            replies = [m for m in (page or {}).get("messages") or [] if m.get("ts") != thread_ts]  # the parent leads
            rows += await self.store(team, tenant, channel_id, replies)
            cursor = ((page or {}).get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return rows

    async def store(self, team: str, tenant: str, channel_id: str, messages: list[dict]) -> int:
        """Insert the messages as raw_events rows; a delivery_id already stored is skipped. New rows."""
        values = []
        for message in messages:
            delivery_id, name, occurred, body = as_delivery(team, channel_id, message)
            values.append({"source": SOURCE, "delivery_id": delivery_id, "account_id": team, "tenant": tenant,
                           "event_name": name, "origin": "reconcile", "occurred_at": occurred,
                           "envelope": {"type": SOURCE, "body": body}})
        if not values:
            return 0
        async with self._sessions.begin() as session:
            result = await session.execute(insert(RawEvent).values(values)
                                           .on_conflict_do_nothing(index_elements=["source", "delivery_id"])
                                           .returning(RawEvent.id))
            return len(result.all())

    async def save_cursor(self, tenant: str, stream: str, position: str) -> None:
        statement = insert(Cursor).values(source=SOURCE, tenant=tenant, stream=stream, position=position)
        async with self._sessions.begin() as session:
            await session.execute(statement.on_conflict_do_update(
                index_elements=["source", "tenant", "stream"],
                set_={"position": statement.excluded.position, "updated_at": func.now()}))

    def _caller(self, team: str, tenant: str, token: str, stopping: asyncio.Event):
        async def call(method: str, params: dict) -> dict | None:
            while True:
                if stopping.is_set():
                    raise Stopped()
                try:
                    return await api(self.limits, self.exchange, tenant, team, token, method,
                                     {k: v for k, v in params.items() if v != ""})
                except Throttled as limit:  # nothing is held here, so wait it out
                    LOG.info("Slack %s for %s is rate limited; waiting %.0fs", method, team, limit.retry_after)
                    await wait_any(stopping, timeout=limit.retry_after)
        return call

    def status(self) -> dict[str, Any]:
        return {"running": self.current, "last": self.last}
