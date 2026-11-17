"""enrich:jira (EMBER-66): turns Jira rows in raw_events into source_records.

What each event becomes and what it fetches is docs/ingestion/jira-webhooks.md. Moved from
services/jira-ingestion/enrich.py onto QueueWorker (EMBER-160), which brings:

- claiming in the source's order, `occurred_at` then `id`, and copying `raw_events.occurred_at` to
  `source_records.occurred_at` (EMBER-63). Intake sets it from the payload with the doc's rules, so
  the worker no longer recomputes it;
- claiming `batch` rows per transaction, one savepoint per row;
- the runtime's failure classes: a rate limit sets the row aside until Jira allows the next call, a
  5xx or network error backs off, and a refused token sets it aside for an hour.

Unchanged: an event that names an issue not yet stored fetches that issue with the token Connect
Jira stored for the row's site (atlassian.py), and only for rows of the tenant that connected the
site. A row with no such token waits, retried with backoff: connecting the site is the only way in.
Every call waits on and updates the site's shared `rate_limits` row (work/rate_limits.py).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ember.connectors.jira.atlassian import LIMITS, SOURCE, OAuthClient, SiteCredentials, bearer_headers, issue_url
from ember.core import http
from ember.core.db import Database
from ember.core.errors import Permanent, Transient
from ember.core.models import RawEvent, SourceRecord
from ember.work.queue_worker import QueueWorker
from ember.work.rate_limits import SharedRateLimit
from source_credentials import KeyRing

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


def kind_of(event: str) -> str | None:
    """source_records kind for a webhookEvent, or None for events that produce no record."""
    if event in ("jira:issue_created", "jira:issue_updated", "jira:issue_deleted"):
        return "issue"
    kind = event.rpartition("_")[0]   # comment_created -> comment, issue_property_set -> issue_property
    return kind if kind in PAYLOAD_KEY else None


def _entity_time(body: dict) -> str | None:
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


def describe(body: dict) -> tuple[str, str, str, list[str]] | None:
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


class JiraEnrich(QueueWorker[RawEvent]):
    """enrich:jira. Without the api ring or the OAuth client, rows that need a call are set aside, the rest still go."""

    channel = "raw_events"
    model = RawEvent
    connections_per_job = 2  # the claim's transaction, and one at a time for the credential or rate_limits

    def __init__(self, db: Database, *, ring: KeyRing | None, client: OAuthClient | None, missing: str = "",
                 exchange: http.Exchange = http.urllib_exchange, clock: Callable[[], float] = time.time,
                 concurrency: int = 4, batch: int = 10, poll_seconds: float = 30.0) -> None:
        super().__init__(db, name="enrich:jira", concurrency=concurrency, batch=batch, poll_seconds=poll_seconds)
        self.ring = ring
        self.client = client
        self.missing = missing
        self.exchange = exchange
        self.token_clock = clock
        self.sites: SiteCredentials | None = None
        self.limits = SharedRateLimit(SOURCE, LIMITS, lambda: self.sessions)

    async def start(self) -> None:
        await super().start()
        if self.ring is not None and self.client is not None:
            assert self.engine is not None
            self.sites = SiteCredentials(self.engine, self.ring, self.client, exchange=self.exchange,
                                         clock=self.token_clock)

    def pending(self) -> Select:
        return (select(RawEvent)
                .where(RawEvent.source == SOURCE, RawEvent.enriched_at.is_(None), RawEvent.tenant.is_not(None))
                .order_by(RawEvent.occurred_at, RawEvent.id))

    async def process(self, session: AsyncSession, raw: RawEvent) -> None:
        """Write the row's source_record and set enriched_at, in the claim's transaction."""
        body = raw.envelope["body"]
        described = describe(body)
        if described is not None:
            kind, object_id, version, issue_ids = described
            record: dict = {"payload": body}
            if version.startswith("deleted:"):
                record["deleted"] = True
            issues = {}
            for issue_id in issue_ids:
                if not await self.issue_stored(session, raw.tenant, issue_id):
                    issue = await self.fetch_issue(raw, issue_id)
                    if issue is not None:
                        issues[issue_id] = issue
            if issues:
                record["issues"] = issues
            await session.execute(
                insert(SourceRecord)
                .values(source=SOURCE, tenant=raw.tenant, kind=kind, object_id=object_id, version=version,
                        occurred_at=raw.occurred_at, record=record, raw_event_id=raw.id)
                .on_conflict_do_nothing(index_elements=["source", "tenant", "object_id", "version"])
            )
        # now() is the transaction's start, the claim, before any API call; clock_timestamp() is the write
        raw.enriched_at = func.clock_timestamp()

    async def issue_stored(self, session: AsyncSession, tenant: str, issue_id: str) -> bool:
        found = await session.scalar(
            select(SourceRecord.id)
            .where(SourceRecord.source == SOURCE, SourceRecord.tenant == tenant,
                   SourceRecord.kind == "issue", SourceRecord.object_id == issue_id)
            .limit(1)
        )
        return found is not None

    async def fetch_issue(self, raw: RawEvent, issue_id: str) -> dict | None:
        """GET one issue with the site's token. A 403 or 404 (no access, or deleted since) returns None."""
        site = raw.account_id
        if not site:
            raise Permanent("the row names no Jira site")
        if self.sites is None:
            raise Permanent(f"{self.missing}: this event needs a Jira API call")
        credential = await self.sites.fresh(site, tenant=raw.tenant)
        if credential is None:  # retried with backoff until someone connects the site for this tenant
            raise Transient(f"no Connect Jira token for site {site} and tenant {raw.tenant}")
        exchange = self.limits.exchange(raw.tenant, f"https://{site}", self.exchange)
        response = await asyncio.to_thread(http.exchange_with_retry, exchange, "GET",
                                           issue_url(credential["cloud_id"], issue_id),
                                           bearer_headers(credential["access_token"]), None, limits=LIMITS)
        if response.status in (403, 404):
            return None
        return http.read_json(response, f"GET issue {issue_id}", LIMITS)
