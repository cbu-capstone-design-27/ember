"""enrich:slack (EMBER-67): turns Slack rows in raw_events into source_records.

What each event becomes and what it fetches is docs/ingestion/slack-webhooks.md. Rebuilt on the
backend's runtime (EMBER-160) rather than moved from services/slack-ingestion/enrich.py:

- QueueWorker claims in Slack's order, `occurred_at` then `id`, which intake set by the doc's rule.
- Every call uses the bot token Connect Slack stored for the row's workspace (EMBER-148), and only for
  rows of the tenant that connected it. A row waits, unclaimed, until its workspace is connected, and
  the next poll picks it up. Slack refusing a token, app_uninstalled, or a tokens_revoked naming our
  bot disconnects the workspace, unless an owner reconnected it since.
- Calls go through core/http (retries, a 429's Retry-After) and wait on one shared `rate_limits` row
  per workspace and method.
- People, channels and thread parents are looked up in source_records first and fetched only when
  missing; what is fetched is stored beside the event's own record, so the next row finds it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlencode

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ember.connectors.slack.webhook import slack_time
from ember.core import http
from ember.core.credentials import Credentials, connected, disconnect
from ember.core.db import Database
from ember.core.errors import ConfigError, Permanent, Transient
from ember.core.models import RawEvent, SourceRecord
from ember.work.queue_worker import QueueWorker
from ember.work.rate_limits import SharedRateLimit
from source_credentials import ENV

LOG = logging.getLogger("ember.slack")
SOURCE = "slack"
API = "https://slack.com/api"
LIFECYCLE = ("app_uninstalled", "tokens_revoked")  # claimed connected or not: they need no token
# A lookup that finds nothing leaves that part of the record empty; the record is still written
MISSES = {"user_not_found", "users_not_found", "channel_not_found", "thread_not_found", "message_not_found",
          "not_in_channel"}
REFUSED = {"not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired"}


class SlackLimits(http.Limits):  # the default rule: a 429 with Retry-After
    name = "Slack"


LIMITS = SlackLimits()


class TokenRefused(Permanent):
    """Slack refused the workspace's bot token: it was revoked, or the app uninstalled."""


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _reply_parent(message: dict) -> str | None:
    thread_ts = _str(message.get("thread_ts"))
    return thread_ts if thread_ts and thread_ts != message.get("ts") else None


def _version(body: dict, event: dict, when: datetime) -> str:
    """For events that aren't messages: event_time is whole seconds, so the event_id keeps two events about
    one object in the same second apart, while a retry (same event_id) stays one row."""
    seconds = body.get("event_time")
    base = str(seconds) if isinstance(seconds, int) else _str(event.get("event_ts")) or f"{when.timestamp():.6f}"
    return f"{base}:{body['event_id']}" if _str(body.get("event_id")) else base


def _channel_id(event: dict) -> str:
    """channel_created and *_rename send a channel object; the others send its id."""
    channel = event.get("channel")
    channel_id = _str(_dict(channel).get("id")) if isinstance(channel, dict) else _str(channel)
    if channel_id is None:
        raise Permanent(f"{event.get('type')} without a channel id")
    return channel_id


# Channel events: the fields each sets besides the channel object it sends (slack-webhooks.md, Everything else)
CHANNEL_EVENTS = {
    "channel_rename": {}, "group_rename": {},
    "channel_archive": {"is_archived": True}, "group_archive": {"is_archived": True},
    "channel_unarchive": {"is_archived": False}, "group_unarchive": {"is_archived": False},
    "channel_deleted": {"is_deleted": True},
}
# System messages that change a channel field (slack-webhooks.md, Messages)
SYSTEM_FIELDS = {"channel_topic": "topic", "group_topic": "topic", "channel_purpose": "purpose",
                 "group_purpose": "purpose", "channel_name": "name", "group_name": "name"}


def channel_change(event: dict) -> tuple[str, dict] | None:
    """(channel id, fields) that an event changes on its channel, or None."""
    if event.get("type") in CHANNEL_EVENTS:
        return _channel_id(event), {**_dict(event.get("channel")), **CHANNEL_EVENTS[event["type"]]}
    field = SYSTEM_FIELDS.get(event.get("subtype")) if event.get("type") == "message" else None
    value = event.get(field) if field else None
    if not _str(event.get("channel")) or not isinstance(value, str) or (field == "name" and not value):
        return None
    if field == "name":
        return event["channel"], {"name": value}
    last_set = slack_time(event.get("ts"))  # as conversations.info shows a topic or purpose
    return event["channel"], {field: {"value": value, "creator": _str(event.get("user")) or "",
                                      "last_set": int(last_set.timestamp()) if last_set else 0}}


class Job:
    """One row's lookups and the records they produce, in the claim's transaction."""

    def __init__(self, worker: SlackEnrich, session: AsyncSession, raw: RawEvent, token: str) -> None:
        self.worker, self.session, self.raw, self.token = worker, session, raw, token
        self.records: list[tuple[str, str, str, datetime, dict]] = []

    def add(self, kind: str, object_id: str, version: str, when: datetime, record: dict) -> None:
        self.records.append((kind, object_id, version, when, record))

    async def stored(self, kind: str, object_id: str, *, at: datetime | None = None) -> dict | None:
        """The newest stored record of that object (at or before `at`, if given), or None."""
        query = (select(SourceRecord.record)
                 .where(SourceRecord.source == SOURCE, SourceRecord.tenant == self.raw.tenant,
                        SourceRecord.kind == kind, SourceRecord.object_id == object_id)
                 .order_by(SourceRecord.occurred_at.desc(), SourceRecord.id.desc()).limit(1))
        if at is not None:
            query = query.where(SourceRecord.occurred_at <= at)
        return await self.session.scalar(query)

    async def call(self, method: str, params: dict, field: str):
        """One Web API call's `field`, or None when Slack found nothing."""
        exchange = self.worker.limits.exchange(self.raw.tenant, f"{self.raw.account_id}/{method}", self.worker.exchange)
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": http.USER_AGENT}
        response = await asyncio.to_thread(http.exchange_with_retry, exchange, "GET",
                                           f"{API}/{method}?{urlencode(params)}", headers, None, limits=LIMITS)
        body = http.read_json(response, method, LIMITS)
        if body.get("ok"):
            return body.get(field)
        error = body.get("error", "no error code")
        if error in MISSES:
            return None
        if error in REFUSED:
            raise TokenRefused(f"{method}: {error}")
        raise (Permanent if error == "missing_scope" else Transient)(f"{method}: {error}")

    async def user(self, user_id: str | None, when: datetime, *, fetch: bool = True) -> dict | None:
        if user_id is None:
            return None  # a bot message, or a delete Slack sent bare
        user = await self.stored("user", user_id)
        if user is None and fetch and (user := await self.call("users.info", {"user": user_id}, "user")):
            self.add("user", user_id, str(user.get("updated") or 0), slack_time(user.get("updated")) or when, user)
        return user

    async def fetch_channel(self, channel_id: str) -> dict | None:
        return await self.call("conversations.info", {"channel": channel_id}, "channel")

    async def channel(self, channel_id: str, when: datetime) -> dict | None:
        channel = await self.stored("channel", channel_id)
        if channel is None and (channel := await self.fetch_channel(channel_id)):
            updated = channel.get("updated")  # epoch milliseconds
            stamp = slack_time(updated / 1000) if isinstance(updated, int) else slack_time(channel.get("created"))
            self.add("channel", channel_id, str(updated or channel.get("created") or 0), stamp or when, channel)
        return channel

    async def change_channel(self, channel_id: str, fields: dict, version: str, when: datetime) -> dict:
        """The channel after a change that carries only its fields, applied onto the channel as stored at
        or before the change. A late change never touches newer rows, so the newest row stays current."""
        base = (await self.stored("channel", channel_id, at=when)
                or (None if fields.get("is_deleted") else await self.fetch_channel(channel_id))
                or {"id": channel_id})
        channel = {**base, **fields}
        self.add("channel", channel_id, version, when, channel)
        return channel

    async def parent(self, channel_id: str, thread_ts: str, when: datetime) -> dict | None:
        object_id = f"{channel_id}/{thread_ts}"
        stored = await self.stored("message", object_id)
        if stored is not None:
            return stored.get("message")
        messages = await self.call("conversations.replies", {"channel": channel_id, "ts": thread_ts, "limit": 1},
                                   "messages")
        parent = messages[0] if isinstance(messages, list) and messages else None
        if not isinstance(parent, dict) or parent.get("ts") != thread_ts:
            return None
        parent = {"channel": channel_id, **parent}
        version = _str(_dict(parent.get("edited")).get("ts")) or thread_ts
        self.add("message", object_id, version, slack_time(version) or when, message_record(None, parent))
        return parent


def message_record(payload, message, *, deleted=False, user=None, channel=None, thread_parent=None) -> dict:
    return {"payload": payload, "message": message, "deleted": deleted, "user": user, "channel": channel,
            "thread_parent": thread_parent}


async def message(job: Job, body: dict, event: dict) -> None:
    """A message event, in any of its shapes (slack-webhooks.md, Messages)."""
    channel_id = _str(event.get("channel"))
    if channel_id is None:
        raise Permanent("message event without a channel")
    when, subtype, nested = job.raw.occurred_at, event.get("subtype"), _dict(event.get("message"))
    deleted, parent = False, None
    if subtype == "message_changed":            # event.ts is when it was edited
        ts, msg = _str(nested.get("ts")), nested
        version = _str(_dict(nested.get("edited")).get("ts")) or _str(event.get("ts")) or ts
        thread_ts = _reply_parent(nested)
    elif subtype == "message_deleted":          # event.ts is when it was deleted
        ts, msg, deleted, thread_ts = _str(event.get("deleted_ts")), event.get("previous_message"), True, None
        version = f"deleted:{_str(event.get('ts')) or ts}"
    elif subtype == "message_replied" or (subtype is None and nested.get("ts")
                                          and nested.get("thread_ts") == nested.get("ts")):
        # A parent's thread fields; Slack sends it without its subtype, hence the nested parent test
        ts, msg, thread_ts = _str(nested.get("ts")), nested, None
        version = _str(event.get("event_ts")) or _str(event.get("ts"))
    else:                                       # new, reply, thread_broadcast, bot, file and system messages
        ts, msg, thread_ts = _str(event.get("ts")), event, _reply_parent(event)
        version = ts
        if subtype == "thread_broadcast":  # root is a copy of the parent
            parent = _dict(event.get("root")) or None
    if ts is None or version is None:
        raise Permanent(f"{subtype or 'message'} event without its ts")
    change = channel_change(event)
    if change is not None:
        channel = await job.change_channel(*change, version, when)
    else:
        channel = await job.channel(channel_id, when)
    user = await job.user(_str(_dict(msg).get("user")), when)
    if parent is None and thread_ts:
        parent = await job.parent(channel_id, thread_ts, when)
    job.add("message", f"{channel_id}/{ts}", version, when,
            message_record(body, msg, deleted=deleted, user=user, channel=channel, thread_parent=parent))


async def user_event(job: Job, body: dict, event: dict) -> None:
    """user_change and team_join: the user as of user.updated. team_join may send no profile."""
    user = _dict(event.get("user"))
    if not _str(user.get("id")):
        raise Permanent(f"{event.get('type')} without user.id")
    if "profile" not in user:
        user = await job.call("users.info", {"user": user["id"]}, "user") or user
    updated, when = user.get("updated"), job.raw.occurred_at
    version = str(updated) if updated else _version(body, event, when)
    job.add("user", user["id"], version, slack_time(updated) or when, user)


async def channel_event(job: Job, body: dict, event: dict) -> None:
    when = job.raw.occurred_at
    version = _version(body, event, when)
    if event.get("type") == "channel_created":  # conversations.info adds privacy, topic and purpose
        channel_id = _channel_id(event)
        job.add("channel", channel_id, version, when,
                {**_dict(event.get("channel")), **(await job.fetch_channel(channel_id) or {})})
    else:
        await job.change_channel(*channel_change(event), version, when)


async def membership(job: Job, body: dict, event: dict) -> None:
    """member_joined_channel / member_left_channel. A leave is a tombstone and fetches nothing."""
    joined = event.get("type") == "member_joined_channel"
    channel_id, user_id, when = _channel_id(event), _str(event.get("user")), job.raw.occurred_at
    if user_id is None:
        raise Permanent(f"{event.get('type')} without a user")
    user = await job.user(user_id, when, fetch=joined)
    job.add("membership", f"{channel_id}/{user_id}", _version(body, event, when), when,
            {"payload": body, "user": user, "channel": channel_id, "member": joined,
             "inviter": _str(event.get("inviter"))})


HANDLERS = {
    "message": message,
    "user_change": user_event, "team_join": user_event,
    "channel_created": channel_event, **{kind: channel_event for kind in CHANNEL_EVENTS},
    "member_joined_channel": membership, "member_left_channel": membership,
}

# A version already stored is left alone, so reprocessing changes nothing. One exception: a message's own
# delivery replaces the copy of that version a reply fetched as its thread_parent (payload null)
REPLACES_FETCHED_COPY = text("source_records.kind = 'message' and source_records.record -> 'payload' = 'null'::jsonb"
                             " and excluded.record -> 'payload' <> 'null'::jsonb")


class SlackEnrich(QueueWorker[RawEvent]):
    channel = "raw_events"
    model = RawEvent
    connections_per_job = 2  # the claim's transaction, and the rate_limits row beside it

    def __init__(self, db: Database, credentials: Credentials, *, exchange: http.Exchange = http.urllib_exchange,
                 concurrency: int = 4, batch: int = 10, poll_seconds: float = 30.0) -> None:
        super().__init__(db, name="enrich:slack", concurrency=concurrency, batch=batch, poll_seconds=poll_seconds)
        self.credentials = credentials
        self.exchange = exchange
        self.limits = SharedRateLimit(SOURCE, LIMITS, lambda: self.sessions)

    async def start(self) -> None:
        """Each workspace's bot token is sealed with the api ring: without it, nothing here can run."""
        if self.credentials.ring("api") is None:
            raise ConfigError(f"{ENV['api']} is not set")
        await super().start()

    def pending(self) -> Select:
        return (select(RawEvent)
                .where(RawEvent.source == SOURCE, RawEvent.enriched_at.is_(None), RawEvent.tenant.is_not(None),
                       or_(RawEvent.event_name.in_(LIFECYCLE),
                           connected(SOURCE, RawEvent.account_id, RawEvent.tenant)))
                .order_by(RawEvent.occurred_at, RawEvent.id))

    async def process(self, session: AsyncSession, raw: RawEvent) -> None:
        body = raw.envelope["body"]
        event = _dict(body.get("event")) if body.get("type") == "event_callback" else {}
        handler = HANDLERS.get(event.get("type"))
        if event.get("type") in LIFECYCLE:
            await self.lifecycle(session, raw, event)
        elif handler is not None:  # reactions, app_rate_limited and the rest make no record yet
            read_at = await session.scalar(select(func.clock_timestamp()))
            secret = await self.credentials.open(SOURCE, raw.account_id, "api", tenant=raw.tenant)
            token = _str(_dict(secret).get("bot_token"))
            if token is None:  # disconnected since the claim; the claim skips it until a reconnect
                raise Transient(f"workspace {raw.account_id} has no bot token for tenant {raw.tenant}")
            job = Job(self, session, raw, token)
            try:
                await handler(job, body, event)
            except TokenRefused as error:  # the row stays pending, and unclaimed until a reconnect
                if await disconnect(session, SOURCE, raw.account_id, stored_before=read_at):
                    LOG.error("%s; disconnected Slack workspace %s, whose rows wait for an owner to reconnect it",
                              error, raw.account_id)
                return
            for kind, object_id, version, when, record in job.records:
                statement = insert(SourceRecord).values(
                    source=SOURCE, tenant=raw.tenant, kind=kind, object_id=object_id, version=version,
                    occurred_at=when, record=record, raw_event_id=raw.id)
                await session.execute(statement.on_conflict_do_update(
                    index_elements=["source", "tenant", "object_id", "version"],
                    set_={"record": statement.excluded.record, "occurred_at": statement.excluded.occurred_at,
                          "raw_event_id": statement.excluded.raw_event_id},
                    where=REPLACES_FETCHED_COPY))
        raw.enriched_at = func.clock_timestamp()

    async def lifecycle(self, session: AsyncSession, raw: RawEvent, event: dict) -> None:
        """app_uninstalled, or tokens_revoked naming our bot, ends the connection, unless an owner
        reconnected after Slack sent it. Our app has one bot per workspace, so when the stored bot id
        is unknown, any revoked bot token is ours. Revoked user tokens change nothing: Ember holds none."""
        if event["type"] == "tokens_revoked":
            bots = _dict(event.get("tokens")).get("bot") or []
            if not bots:
                return
            try:
                secret = await self.credentials.open(SOURCE, raw.account_id, "api", tenant=raw.tenant)
            except Exception:  # an unreadable credential can't say whose bot it was
                secret = None
            ours = _str(_dict(secret).get("bot_user_id"))
            if ours is not None and ours not in bots:
                return
        if await disconnect(session, SOURCE, raw.account_id, stored_before=raw.occurred_at):
            LOG.warning("%s: disconnected Slack workspace %s; its records stay, and an owner can reconnect it",
                        event["type"], raw.account_id)

