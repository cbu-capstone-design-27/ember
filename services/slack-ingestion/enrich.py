"""enrich:slack, the Slack enrich worker (EMBER-67).

Reads Slack rows from Postgres raw_events, adds what the Events API payload lacks, and writes
source_records. The spec is docs/ingestion/slack-webhooks.md (EMBER-70): what each event carries,
what is fetched, and each record's object_id, version and occurred_at. The schema is
docs/ingestion/architecture.md (Storage).

How it runs:
- Idle, it waits on LISTEN raw_events. The database sends NOTIFY raw_events '<source>' on every
  insert (the trigger EMBER-62 adds). A poll every ENRICH_POLL_SECONDS catches anything missed.
- ENRICH_CONCURRENCY asyncio runners each claim one pending row (enriched_at is null, FOR UPDATE
  SKIP LOCKED), enrich it, write its records and set enriched_at, in the row's own transaction.
  A crash releases the row, and it runs again.
- At start the runners claim straight away, so rows inserted while it was stopped are picked up.
- Ctrl+C (SIGINT) or SIGTERM: stop claiming, finish the jobs in flight, exit. A second Ctrl+C
  exits at once; an unfinished row stays pending.

Scope: message events (new messages, replies, broadcasts, message_replied, edits, deletes, system
messages). The user and channel a message names are cached as records of kind user and channel,
fetched with users.info / conversations.info only when no record exists yet. A reply's parent
comes from conversations.replies when it isn't stored. Other events are marked enriched and skipped.

Every Slack call goes through pull_test.SlackClient (EMBER-52's pacing, throttle and backoff), with
its gates kept in table rate_limits (pg_gates.py).

    python3 enrich.py                              # run the worker
    python3 enrich.py seed FILE --tenant TENANT    # store deliveries as raw_events rows

seed stands in for intake until EMBER-62: it stores Events API deliveries (a JSON fixture, or
JSONL such as receiver.py's stdout with FILE = -) and sends the NOTIFY itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import RawEvent, SourceRecord
from pg_gates import PgMethodGates
from pull_test import (
    AUTH_ERRORS,
    DEFAULT_RATE_MODE,
    PullError,
    SlackApiError,
    SlackClient,
    bot_token,
    make_client,
    rate_table,
    resolve_rate_mode,
    urllib_exchange,
)

LOG = logging.getLogger("slack-enrich")

SOURCE = "slack"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16
DEFAULT_CHANNEL = "raw_events"
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 8
ERROR_TEXT_LIMIT = 500

# A lookup that finds nothing leaves that context empty. The record is still written.
LOOKUP_MISSES = {
    "user_not_found",
    "users_not_found",
    "channel_not_found",
    "thread_not_found",
    "message_not_found",
    "not_in_channel",
}
# Errors no retry can fix. The row is left for a person, with enrich_error saying why.
PERMANENT_ERRORS = AUTH_ERRORS | {"missing_scope"}

# The ontology's tenant rule (services/pipeline/ontology/sources.py, subgraph_id)
TENANT = re.compile(r"[a-z0-9][a-z0-9-]*")


class ConfigError(Exception):
    pass


class Permanent(Exception):
    """Retrying cannot enrich this row. It is left with enrich_attempts at the maximum."""


# ---- Configuration ----------------------------------------------------------------------------


def database_urls(raw: str) -> tuple[str, str]:
    """(SQLAlchemy URL, libpq URL) for DATABASE_URL.

    Accepts postgres://, postgresql:// and postgresql+<driver>://, as CloudNativePG's Secret,
    compose and SQLAlchemy write it. Query parameters such as sslmode pass through.
    """
    value = (raw or "").strip()
    if not value:
        raise ConfigError("Set DATABASE_URL to the ingestion Postgres, e.g. postgresql://ember:...@localhost:5432/ember")
    parts = urlsplit(value)
    if parts.scheme.split("+", 1)[0] not in ("postgres", "postgresql"):
        raise ConfigError("DATABASE_URL must be a postgresql:// URL")
    return (
        urlunsplit(parts._replace(scheme="postgresql+psycopg")),
        urlunsplit(parts._replace(scheme="postgresql")),
    )


def env_number(name: str, default, low, high, kind=int):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be from {low} to {high}")
    return value


def env_channel() -> str:
    channel = os.environ.get("ENRICH_CHANNEL", "").strip() or DEFAULT_CHANNEL
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", channel):
        raise ConfigError("ENRICH_CHANNEL must be a lowercase identifier such as raw_events")
    return channel


# ---- What a delivery says ---------------------------------------------------------------------


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value) -> dict | None:
    return value if isinstance(value, dict) else None


def slack_time(value) -> datetime | None:
    """A Slack ts ("1717000001.000200") or epoch seconds, as an exact UTC datetime."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        whole, _, fraction = value.strip().partition(".")
        if not whole.isdigit() or (fraction and not fraction.isdigit()):
            return None
        micros = int((fraction + "000000")[:6])
        return datetime.fromtimestamp(int(whole), tz=timezone.utc) + timedelta(microseconds=micros)
    return None


def millis_time(value) -> datetime | None:
    """Epoch milliseconds (a channel's "updated") as a UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _reply_parent(message: dict) -> str | None:
    """The parent's ts when ``message`` is a thread reply, else None."""
    thread_ts = _str(message.get("thread_ts"))
    return thread_ts if thread_ts and thread_ts != message.get("ts") else None


@dataclass(frozen=True)
class Delivery:
    """What one Slack delivery says about one message, before anything is fetched."""

    shape: str  # new, reply, broadcast, replied, changed, deleted
    channel: str
    object_id: str  # "<channel>/<ts>"
    version: str
    occurred_at: datetime
    message: dict | None  # the message as of this version; None for a delete Slack sent bare
    user: str | None  # whose profile to attach
    thread_ts: str | None  # the parent's ts when the message is a reply
    parent: dict | None  # a copy of the parent the payload already carries (thread_broadcast's root)


def describe(body) -> Delivery | None:
    """The message a delivery is about (slack-webhooks.md, Messages), or None if it isn't one.

    None means the row is skipped: other event types, app_rate_limited, anything that is not an
    Events API message. Raises Permanent for a message event without the fields it needs.
    """
    if not isinstance(body, dict) or body.get("type") != "event_callback":
        return None
    event = _dict(body.get("event"))
    if event is None or event.get("type") != "message":
        return None
    channel = _str(event.get("channel"))
    if channel is None:
        raise Permanent("message event without a channel")
    # The ordering value (slack-webhooks.md, Ordering value): event_ts, then ts, then event_time
    occurred = slack_time(event.get("event_ts")) or slack_time(event.get("ts")) or slack_time(body.get("event_time"))
    if occurred is None:
        raise Permanent("message event without event_ts, ts or event_time")
    subtype = event.get("subtype")
    nested = _dict(event.get("message"))

    if subtype == "message_changed":
        ts = _str(nested.get("ts")) if nested else None
        if ts is None:
            raise Permanent("message_changed without message.ts")
        edited = _dict(nested.get("edited")) or {}
        version = _str(edited.get("ts")) or _str(event.get("ts")) or ts
        when = slack_time(event.get("ts")) or occurred  # event.ts is when it was edited
        return Delivery(
            "changed", channel, f"{channel}/{ts}", version, when, nested, _str(nested.get("user")), _reply_parent(nested), None
        )

    if subtype == "message_deleted":
        deleted_ts = _str(event.get("deleted_ts"))
        if deleted_ts is None:
            raise Permanent("message_deleted without deleted_ts")
        previous = _dict(event.get("previous_message"))
        deleted_at = _str(event.get("ts")) or _str(event.get("event_ts")) or deleted_ts
        when = slack_time(event.get("ts")) or occurred  # event.ts is when it was deleted
        user = _str(previous.get("user")) if previous else None
        return Delivery("deleted", channel, f"{channel}/{deleted_ts}", f"deleted:{deleted_at}", when, previous, user, None, None)

    # message_replied refreshes a parent's thread fields. Slack documents that it can arrive
    # without its subtype, so it is also recognized by a nested parent (thread_ts == ts).
    if subtype == "message_replied" or (
        subtype is None and nested is not None and _str(nested.get("ts")) and nested.get("thread_ts") == nested.get("ts")
    ):
        ts = _str(nested.get("ts")) if nested else None
        version = _str(event.get("event_ts")) or _str(event.get("ts"))
        if ts is None or version is None:
            raise Permanent("message_replied without message.ts or event_ts")
        return Delivery("replied", channel, f"{channel}/{ts}", version, occurred, nested, _str(nested.get("user")), None, None)

    ts = _str(event.get("ts"))
    if ts is None or slack_time(ts) is None:
        raise Permanent("message event without a valid ts")
    thread_ts = _reply_parent(event)
    user = _str(event.get("user"))
    if subtype == "thread_broadcast":
        return Delivery("broadcast", channel, f"{channel}/{ts}", ts, occurred, event, user, thread_ts, _dict(event.get("root")))
    # A new message, bot_message, file_share, me_message, or a system message (channel_join, ...)
    shape = "reply" if thread_ts else "new"
    return Delivery(shape, channel, f"{channel}/{ts}", ts, occurred, event, user, thread_ts, None)


# ---- Filling in what the payload lacks --------------------------------------------------------


@dataclass(frozen=True)
class NewRecord:
    """A source_records row before the worker adds source, tenant and raw_event_id."""

    kind: str
    object_id: str
    version: str
    occurred_at: datetime
    record: dict


class Cache(Protocol):
    async def latest(self, kind: str, object_id: str) -> dict | None:
        """The newest stored record of that kind and object id, or None."""


# A Slack Web API call: method, params -> the response object
Call = Callable[[str, dict], Awaitable[dict]]


def message_record(payload, message, *, deleted, user=None, channel=None, thread_parent=None) -> dict:
    """source_records.record for a message: the delivery as received plus what was fetched."""
    return {
        "payload": payload,
        "message": message,
        "deleted": deleted,
        "user": user,
        "channel": channel,
        "thread_parent": thread_parent,
    }


class Context:
    """Users, channels and thread parents for one delivery: stored records first, then the API.

    What it fetches is kept in ``fetched``, written with the delivery so the next one finds it.
    """

    def __init__(self, cache: Cache, call: Call) -> None:
        self._cache = cache
        self._call = call
        self.fetched: list[NewRecord] = []

    async def user(self, user_id: str | None, when: datetime) -> dict | None:
        if user_id is None:
            return None  # a bot message or a delete Slack sent bare
        stored = await self._cache.latest("user", user_id)
        if stored is not None:
            return stored
        user = await self._lookup("users.info", {"user": user_id}, "user")
        if not isinstance(user, dict):
            return None
        updated = user.get("updated")
        self.fetched.append(NewRecord("user", user_id, str(updated or 0), slack_time(updated) or when, user))
        return user

    async def channel(self, channel_id: str, when: datetime) -> dict | None:
        stored = await self._cache.latest("channel", channel_id)
        if stored is not None:
            return stored
        channel = await self._lookup("conversations.info", {"channel": channel_id}, "channel")
        if not isinstance(channel, dict):
            return None
        updated, created = channel.get("updated"), channel.get("created")
        version = str(updated or created or 0)
        occurred = millis_time(updated) or slack_time(created) or when
        self.fetched.append(NewRecord("channel", channel_id, version, occurred, channel))
        return channel

    async def parent(self, channel_id: str, thread_ts: str, when: datetime) -> dict | None:
        object_id = f"{channel_id}/{thread_ts}"
        stored = await self._cache.latest("message", object_id)
        if stored is not None:
            return stored.get("message")
        messages = await self._lookup(
            "conversations.replies", {"channel": channel_id, "ts": thread_ts, "limit": 1}, "messages"
        )
        first = messages[0] if isinstance(messages, list) and messages else None
        if not isinstance(first, dict) or first.get("ts") != thread_ts:
            return None
        parent = dict(first)
        parent.setdefault("channel", channel_id)  # as the backfill does (slack.md, Data shapes)
        edited = _dict(parent.get("edited")) or {}
        version = _str(edited.get("ts")) or thread_ts
        record = message_record(None, parent, deleted=False)
        self.fetched.append(NewRecord("message", object_id, version, slack_time(thread_ts) or when, record))
        return parent

    async def _lookup(self, method: str, params: dict, field: str):
        try:
            response = await self._call(method, params)
        except SlackApiError as exc:
            if exc.error in LOOKUP_MISSES:
                LOG.info("%s found nothing (%s); that context stays empty", method, exc.error)
                return None
            if exc.error in PERMANENT_ERRORS:
                raise Permanent(str(exc)) from exc
            raise
        return response.get(field)


async def enrich_delivery(body, *, cache: Cache, call: Call) -> list[NewRecord]:
    """The records one delivery becomes: what was fetched for it, then the message itself.

    An empty list means the delivery is not a message event and is skipped.
    """
    delivery = describe(body)
    if delivery is None:
        return []
    context = Context(cache, call)
    when = delivery.occurred_at
    channel = await context.channel(delivery.channel, when)
    user = await context.user(delivery.user, when)
    parent = delivery.parent
    if parent is None and delivery.thread_ts:
        parent = await context.parent(delivery.channel, delivery.thread_ts, when)
    record = message_record(
        body,
        delivery.message,
        deleted=delivery.shape == "deleted",
        user=user,
        channel=channel,
        thread_parent=parent,
    )
    return [*context.fetched, NewRecord("message", delivery.object_id, delivery.version, when, record)]


# ---- The worker -------------------------------------------------------------------------------


class StoredRecords:
    """The Cache, read from source_records inside the job's transaction."""

    def __init__(self, session, tenant: str) -> None:
        self._session = session
        self._tenant = tenant

    async def latest(self, kind: str, object_id: str) -> dict | None:
        return await self._session.scalar(
            select(SourceRecord.record)
            .where(
                SourceRecord.source == SOURCE,
                SourceRecord.tenant == self._tenant,
                SourceRecord.kind == kind,
                SourceRecord.object_id == object_id,
            )
            .order_by(SourceRecord.occurred_at.desc(), SourceRecord.id.desc())
            .limit(1)
        )


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_TEXT_LIMIT]


class Worker:
    """LISTEN for new rows, and run up to ``concurrency`` jobs at once until stop() is called."""

    def __init__(
        self,
        database_url: str,
        *,
        token: str,
        exchange=urllib_exchange,
        rate_mode: str = DEFAULT_RATE_MODE,
        rates: dict[str, float] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        channel: str = DEFAULT_CHANNEL,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._alchemy_url, self._libpq_url = database_urls(database_url)
        self._token = token
        self._exchange = exchange
        self._rate_mode = rate_mode
        self._rates = rate_table(rate_mode) if rates is None else dict(rates)
        self.concurrency = concurrency
        self.channel = channel
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._clients: dict[str, SlackClient] = {}
        self._clients_lock = threading.Lock()
        # Rows that just failed, and when they may be tried again (monotonic time). Without this a
        # runner would claim the same row again at once and use up its attempts in milliseconds.
        self._cooldown: dict[int, float] = {}
        self._kicks = [asyncio.Event() for _ in range(concurrency)]
        self.stopping = asyncio.Event()
        self.listening = asyncio.Event()  # set while LISTEN is active
        self.done = 0  # rows finished (enriched or skipped) since start

    async def run(self) -> None:
        self._engine = create_async_engine(
            self._alchemy_url, pool_size=self.concurrency + 1, max_overflow=2, pool_pre_ping=True
        )
        self._sync_engine = create_engine(self._alchemy_url, pool_size=2, max_overflow=self.concurrency, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        listener = asyncio.create_task(self._listen(), name="listen")
        runners = [asyncio.create_task(self._runner(i), name=f"runner-{i}") for i in range(self.concurrency)]
        LOG.info(
            "enrich:slack started: concurrency %s, LISTEN %s, poll every %ss", self.concurrency, self.channel, self.poll_seconds
        )
        try:
            await asyncio.gather(*runners)
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            await self._engine.dispose()
            self._sync_engine.dispose()
            LOG.info("enrich:slack stopped after %s rows", self.done)

    def stop(self) -> None:
        """Stop claiming rows. Jobs already running finish first."""
        self.stopping.set()
        self._kick_all()

    def _kick_all(self) -> None:
        for kick in self._kicks:
            kick.set()

    async def _runner(self, index: int) -> None:
        kick = self._kicks[index]
        while not self.stopping.is_set():
            kick.clear()
            try:
                worked = await self.run_one()
            except Exception:  # the database, not the row: try again after a pause
                LOG.exception("could not claim or finish a row")
                await self._pause(min(self.poll_seconds, 5.0), kick)
                continue
            if not worked:
                await self._pause(self.poll_seconds, kick)

    async def _pause(self, seconds: float, kick: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(kick.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _listen(self) -> None:
        delay = 1.0
        while not self.stopping.is_set():
            try:
                conn = await psycopg.AsyncConnection.connect(self._libpq_url, autocommit=True)
                async with conn:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
                    self.listening.set()
                    self._kick_all()  # rows that arrived while nothing was listening
                    delay = 1.0
                    async for note in conn.notifies():
                        if note.payload in (SOURCE, ""):
                            self._kick_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.listening.clear()
                LOG.warning("LISTEN %s lost (%s); reconnecting in %.0fs", self.channel, type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def _claim(self):
        now = time.monotonic()
        for row_id in [r for r, until in self._cooldown.items() if until <= now]:
            del self._cooldown[row_id]
        stmt = select(RawEvent.id, RawEvent.tenant, RawEvent.event_name, RawEvent.envelope, RawEvent.enrich_attempts).where(
            RawEvent.source == SOURCE,
            RawEvent.tenant.is_not(None),  # unmapped accounts wait for ember-admin map
            RawEvent.enriched_at.is_(None),
            RawEvent.enrich_attempts < self.max_attempts,
        )
        if self._cooldown:
            stmt = stmt.where(RawEvent.id.not_in(list(self._cooldown)))
        return (
            stmt.order_by(RawEvent.id)  # EMBER-63: order by the source ordering column, then id
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    def _backoff(self, row_id: int, attempts: int) -> None:
        """Leave a failed row alone for 2, 4, 8 … seconds, at most one poll interval."""
        self._cooldown[row_id] = time.monotonic() + min(self.poll_seconds, 2.0 ** attempts)

    async def run_one(self) -> bool:
        """Claim one pending Slack row and enrich it. False when nothing is pending."""
        async with self._sessions() as session, session.begin():
            row = (await session.execute(self._claim())).mappings().first()
            if row is None:
                return False
            row_id, tenant = row["id"], row["tenant"]
            envelope = row["envelope"] if isinstance(row["envelope"], dict) else {}
            mark = update(RawEvent).where(RawEvent.id == row_id)
            try:
                async with session.begin_nested():
                    records = await enrich_delivery(
                        envelope.get("body"), cache=StoredRecords(session, tenant), call=self._caller(tenant)
                    )
                    for rec in records:
                        await session.execute(
                            pg_insert(SourceRecord)
                            .values(
                                source=SOURCE,
                                tenant=tenant,
                                kind=rec.kind,
                                object_id=rec.object_id,
                                version=rec.version,
                                occurred_at=rec.occurred_at,
                                record=rec.record,
                                raw_event_id=row_id,
                            )
                            .on_conflict_do_nothing(index_elements=["source", "tenant", "object_id", "version"])
                        )
            except Permanent as exc:
                await session.execute(mark.values(enrich_attempts=self.max_attempts, enrich_error=_error_text(exc)))
                LOG.error("raw_event %s left for a person: %s", row_id, exc)
            except Exception as exc:
                await session.execute(
                    mark.values(enrich_attempts=RawEvent.enrich_attempts + 1, enrich_error=_error_text(exc))
                )
                self._backoff(row_id, row["enrich_attempts"] + 1)
                LOG.warning("raw_event %s failed, retried later: %s", row_id, _error_text(exc))
            else:
                await session.execute(mark.values(enriched_at=func.now(), enrich_error=None))
                self.done += 1
                if records:
                    LOG.info("enriched raw_event %s: %s", row_id, ", ".join(f"{r.kind} {r.object_id}" for r in records))
                else:
                    LOG.info("skipped raw_event %s (%s): not a message event", row_id, row["event_name"])
            return True

    def _caller(self, tenant: str) -> Call:
        client = self._client(tenant)

        async def call(method: str, params: dict) -> dict:
            return await asyncio.to_thread(client.call, method, params)

        return call

    def _client(self, tenant: str) -> SlackClient:
        """One SlackClient per workspace, so each one's rate_limits rows are its own."""
        with self._clients_lock:
            client = self._clients.get(tenant)
            if client is None:
                gates = PgMethodGates(self._sync_engine, tenant, self._rates, sleep=self._sleep)
                client = make_client(self._token, self._exchange, rate_mode=self._rate_mode, gates=gates)
                self._clients[tenant] = client
            return client


def run_async(coro):
    """asyncio.run, with the selector event loop psycopg's async mode needs on Windows."""
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)


async def serve(worker: Worker) -> None:
    """Run the worker until SIGINT or SIGTERM; a second signal exits at once."""
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        if worker.stopping.is_set():
            LOG.warning("second interrupt: exiting now; unfinished rows stay pending")
            os._exit(130)
        LOG.info("stopping: finishing the jobs in flight (Ctrl+C again to exit now)")
        worker.stop()

    if sys.platform == "win32":  # the selector loop on Windows has no add_signal_handler
        previous = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(on_signal))
        try:
            await worker.run()
        finally:
            signal.signal(signal.SIGINT, previous)
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
        try:
            await worker.run()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)


# ---- seed: intake's stand-in ------------------------------------------------------------------


def event_name(body: dict) -> str:
    """raw_events.event_name: the inner event type, plus ":<subtype>" (slack-webhooks.md)."""
    if body.get("type") == "event_callback":
        event = _dict(body.get("event")) or {}
        name = _str(event.get("type")) or "event_callback"
        subtype = _str(event.get("subtype"))
        return f"{name}:{subtype}" if subtype else name
    return _str(body.get("type")) or "unknown"


def raw_event_row(envelope, tenant: str | None) -> dict | None:
    """The raw_events row intake would store for one Events API envelope, or None if it isn't one."""
    if not isinstance(envelope, dict) or envelope.get("type") != SOURCE:
        return None
    body = _dict(envelope.get("body"))
    if body is None:
        return None
    if body.get("type") == "event_callback":
        delivery_id = _str(body.get("event_id"))
    elif body.get("type") == "app_rate_limited":
        delivery_id = f"rate_limited:{body.get('team_id')}:{body.get('minute_rate_limited')}"
    else:
        return None  # a backfill object; reconcile stores those, not intake
    if delivery_id is None:
        return None
    return {
        "source": SOURCE,
        "delivery_id": delivery_id,
        "account_id": _str(body.get("team_id")),
        "tenant": tenant,
        "event_name": event_name(body),
        "origin": "webhook",
        "envelope": envelope,
    }


def read_envelopes(source: str) -> Iterator[dict]:
    """Envelopes from a JSON object, a JSON list or JSONL. "-" streams JSONL from stdin."""
    if source == "-":
        for line in sys.stdin:
            if line.strip():
                yield json.loads(line)
        return
    content = Path(source).read_text(encoding="utf-8").strip()
    if not content:
        return
    try:
        whole = json.loads(content)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        yield whole
    elif isinstance(whole, list):
        yield from (item for item in whole if isinstance(item, dict))
    else:
        for line in content.splitlines():
            if line.strip():
                yield json.loads(line)


def seed(database_url: str, source: str, tenant: str, *, channel: str = DEFAULT_CHANNEL) -> tuple[int, int, int]:
    """Store deliveries as raw_events rows and NOTIFY, as intake will. Returns (added, already stored, ignored)."""
    if not TENANT.fullmatch(tenant):
        raise ConfigError(f"tenant {tenant!r} must match {TENANT.pattern}")
    engine = create_engine(database_urls(database_url)[0])
    added = duplicate = ignored = 0
    try:
        for envelope in read_envelopes(source):
            row = raw_event_row(envelope, tenant)
            if row is None:
                ignored += 1
                continue
            with engine.begin() as conn:
                new_id = conn.execute(
                    pg_insert(RawEvent)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=["source", "delivery_id"])
                    .returning(RawEvent.id)
                ).scalar_one_or_none()
                if new_id is None:
                    duplicate += 1
                    continue
                # Stands in for EMBER-62's trigger; delivered when this transaction commits
                conn.execute(text("select pg_notify(:channel, :payload)"), {"channel": channel, "payload": SOURCE})
                added += 1
    finally:
        engine.dispose()
    return added, duplicate, ignored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="enrich:slack: turn Slack raw_events into source_records (EMBER-67).")
    commands = parser.add_subparsers(dest="command")
    seeding = commands.add_parser("seed", help="Store Events API deliveries as raw_events rows, as intake will.")
    seeding.add_argument("file", help="JSON or JSONL of {type, body} envelopes. - reads JSONL from stdin.")
    seeding.add_argument("--tenant", required=True, help="Tenant to store the rows under, e.g. dev.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        database_url = os.environ.get("DATABASE_URL", "")
        if args.command == "seed":
            added, duplicate, ignored = seed(database_url, args.file, args.tenant, channel=env_channel())
            print(
                f"seeded {added} raw_events rows ({duplicate} already stored, {ignored} not Events API deliveries)",
                file=sys.stderr,
            )
            return 0
        worker = Worker(
            database_url,
            token=bot_token(),
            rate_mode=resolve_rate_mode(None),
            concurrency=env_number("ENRICH_CONCURRENCY", DEFAULT_CONCURRENCY, 1, MAX_CONCURRENCY),
            channel=env_channel(),
            poll_seconds=env_number("ENRICH_POLL_SECONDS", DEFAULT_POLL_SECONDS, 1.0, 3600.0, float),
            max_attempts=env_number("ENRICH_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1, 100),
        )
        run_async(serve(worker))
    except (ConfigError, PullError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
