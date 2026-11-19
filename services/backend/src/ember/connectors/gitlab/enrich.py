"""enrich:gitlab (EMBER-68): turns GitLab rows in raw_events into source_records.

What each event becomes and what it fetches is docs/ingestion/gitlab-webhooks.md. Rebuilt on the
backend's runtime (EMBER-160) rather than moved from services/gitlab-ingestion/enrich.py:

- QueueWorker claims in GitLab's order, `occurred_at` then `id`, which intake set by the doc's rule.
- Only merge_request open, reopen and updates that add commits, and pushes with more commits than
  the 20 GitLab sends, call the API. They use the read_api token Connect GitLab (EMBER-149) stored for
  the row's group, only for rows of the tenant that connected it, and only on the instance it was
  connected on. A row that needs a call while its group isn't connected is set aside and retried.
  GitLab refusing the token (401) disconnects the group, unless an owner reconnected it since.
- Calls go through core/http (retries, a 429's Retry-After, RateLimit-Remaining) and wait on one
  shared `rate_limits` row per group.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ember.core import http
from ember.core.credentials import Credentials, disconnect
from ember.core.db import Database
from ember.core.errors import ConfigError, Permanent
from ember.core.models import RawEvent, SourceRecord
from ember.work.queue_worker import QueueWorker
from ember.work.rate_limits import SharedRateLimit
from source_credentials import ENV

LOG = logging.getLogger("ember.enrich.gitlab")
SOURCE = "gitlab"
API_PREFIX = "/api/v4"
PAGE_SIZE = 100
MAX_PAGES = 200
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
ZERO_SHA = "0" * 40  # before on a new branch, after on a deleted one
DIFF_ACTIONS = ("open", "reopen")  # plus an update that adds commits (oldrev)
APPROVALS = ("approval", "unapproval", "approved", "unapproved")
KINDS = ("merge_request", "issue", "note", "push", "project")
INSERT_CHUNK = 1000


class GitLabLimits(http.Limits):  # a 429, with Retry-After or RateLimit-Reset
    name = "GitLab"


LIMITS = GitLabLimits()


class TokenRefused(Permanent):
    """GitLab refused the group's token: revoked, expired, or its user removed."""


def _time(value: str) -> datetime:
    """GitLab's two time formats: ISO 8601 (REST, and webhooks since 19.0) and `2026-01-16 05:56:25 UTC`."""
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")] + "+00:00"
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stamp(value: str | datetime) -> str:
    """A time as a version: UTC ISO 8601 with milliseconds, like GitLab's own `2026-01-16T05:56:25.000Z`.
    One version gets the same string from a webhook and from REST, so their copies fold together."""
    moment = (_time(value) if isinstance(value, str) else value).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def describe(body: dict) -> tuple[str | None, str | None]:
    """(kind, action) of a body. Webhooks carry object_kind; REST objects (a backfill's rows) don't, so
    the fields only that type has decide."""
    object_kind = body.get("object_kind")
    if object_kind:
        action = (body.get("object_attributes") or {}).get("action")
        return ("issue" if object_kind == "work_item" else object_kind), action
    if "noteable_type" in body:
        return "note", None
    if "iid" in body and "source_branch" in body:
        return "merge_request", None
    if "iid" in body and "issue_type" in body:
        return "issue", None
    if "path_with_namespace" in body:
        return "project", None
    return None, None


def _attributes(body: dict) -> dict:
    """The object itself: object_attributes on a webhook, the body on a REST object."""
    return body.get("object_attributes") or body


def _project_id(kind: str, body: dict) -> int | None:
    if kind == "project":
        return body["id"]
    project = body.get("project")
    if isinstance(project, dict) and project.get("id") is not None:
        return project["id"]
    return body.get("project_id")  # push webhooks, and REST merge requests, issues and notes


def _hidden(kind: str, body: dict) -> bool:
    """Confidential issues and internal notes make no record, in case a webhook has those triggers on."""
    if body.get("event_type") in ("confidential_issue", "confidential_note"):
        return True
    attrs = _attributes(body)
    if kind == "issue":
        return attrs.get("confidential") is True
    if kind == "note":
        return attrs.get("internal") is True or attrs.get("confidential") is True
    return False


def to_records(raw: RawEvent, fetched: dict) -> list[dict]:
    """The source_records rows one delivery becomes: kind, object_id, version and record."""
    body = raw.envelope["body"]
    kind, action = describe(body)
    if kind not in KINDS or _hidden(kind, body):
        return []  # tag push, emoji, pipeline and the other triggers that stay off make no record
    project_id = _project_id(kind, body)
    if project_id is None:
        return []  # group-level work items (epics) reach only group webhooks
    received = stamp(raw.received_at)

    def row(kind: str, object_id: str, version: str, payload: dict, tombstone: bool = False,
            extras: dict | None = None) -> dict:
        record: dict = {"payload": payload}
        if extras:
            record["fetched"] = extras
        if tombstone:
            record["tombstone"] = True
        return {"kind": kind, "object_id": object_id, "version": version, "record": record}

    if kind == "push":
        commits = fetched["compare"] if "compare" in fetched else body.get("commits") or []
        rows = [row("commit", f"{project_id}@{c['id']}", c["id"], c) for c in commits]
        ref = {key: value for key, value in body.items() if key != "commits"}
        if body["after"] == ZERO_SHA:
            rows.append(row("ref", f"{project_id}:{body['ref']}", f"deleted:{received}", ref, tombstone=True))
        else:
            rows.append(row("ref", f"{project_id}:{body['ref']}", body["after"], ref))
        return rows
    if kind == "project":
        return [row("project", str(project_id), stamp(body["last_activity_at"]), body)]
    attrs = _attributes(body)
    object_id = {"merge_request": f"{project_id}!{attrs.get('iid')}", "issue": f"{project_id}#{attrs.get('iid')}",
                 "note": f"{project_id}/note/{attrs.get('id')}"}[kind]
    # GitLab doesn't say approvals bump updated_at, so they version on arrival instead of folding into the last update
    version = f"{action}:{received}" if action in APPROVALS else stamp(attrs["updated_at"])
    return [row(kind, object_id, version, body, extras=fetched)]


def fetch_path(body: dict) -> tuple[str, str] | None:
    """("pages" | "object", path) of the one call a webhook needs (gitlab-webhooks.md, "Event types"), or None."""
    kind, action = describe(body)
    if not body.get("object_kind") or kind not in KINDS or _hidden(kind, body):
        return None
    project_id = _project_id(kind, body)
    if kind == "merge_request":
        attrs = body["object_attributes"]
        if action in DIFF_ACTIONS or (action == "update" and attrs.get("oldrev")):
            return "pages", f"/projects/{project_id}/merge_requests/{attrs['iid']}/diffs"
    if kind == "push" and body["after"] != ZERO_SHA:
        if (body.get("total_commits_count") or 0) <= len(body.get("commits") or []):
            return None
        base = body["before"]
        if base == ZERO_SHA:
            # A new branch compares from the default branch; the first push to an empty project keeps the 20
            default = (body.get("project") or {}).get("default_branch")
            if not default or body["ref"] == f"refs/heads/{default}":
                return None
            base = default
        return "object", f"/projects/{project_id}/repository/compare?{urlencode({'from': base, 'to': body['after']})}"
    return None


def api_root(account_id: str, credential: dict | None) -> tuple[str, str]:
    """(API root, token) from Connect GitLab's credential, which must name the account's own instance."""
    token, base = (credential or {}).get("token"), (credential or {}).get("base_url")
    if not isinstance(token, str) or not token or not isinstance(base, str):
        raise Permanent(f"the Connect GitLab credential for {account_id} has no token or base_url")
    base = base.strip().rstrip("/")
    parts = urlsplit(base)
    if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)) \
            or parts.query or parts.fragment or "@" in parts.netloc:
        raise Permanent(f"the Connect GitLab credential for {account_id} has an unusable base_url")
    # account_id is <instance host>/<group>, so the token only ever goes to the instance it was connected on
    if (parts.hostname or "") != account_id.split("/", 1)[0]:
        raise Permanent(f"the Connect GitLab credential for {account_id} names another instance")
    return base + API_PREFIX, token


def next_link(link_header: str | None) -> str | None:
    for part in (link_header or "").split(","):
        if 'rel="next"' in part and "<" in part and ">" in part:
            return part[part.find("<") + 1 : part.find(">")]
    return None


async def get(limits: SharedRateLimit, exchange: http.Exchange, tenant: str, account_id: str, token: str,
              url: str) -> tuple:
    """(JSON body, response) of one GET with the group's token, on the group's shared rate_limits row."""
    paced = limits.exchange(tenant, account_id, exchange)
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json", "User-Agent": http.USER_AGENT}
    response = await asyncio.to_thread(http.exchange_with_retry, paced, "GET", url, headers, None, limits=LIMITS)
    what = f"GET {urlsplit(url).path}"
    if response.status == 401:
        raise TokenRefused(f"{what} failed (401)")
    return http.read_json(response, what, LIMITS), response


async def pages(limits: SharedRateLimit, exchange: http.Exchange, tenant: str, account_id: str, token: str,
                api: str, path: str, params: dict | None = None):
    """Each page of a list, following rel="next" only within `api`, so the token never goes elsewhere."""
    url: str | None = f"{api}{path}?{urlencode({**(params or {}), 'per_page': PAGE_SIZE})}"
    for _ in range(MAX_PAGES):
        body, response = await get(limits, exchange, tenant, account_id, token, url)
        yield body
        url = next_link(response.headers.get("Link") or response.headers.get("link"))
        if url is None:
            return
        if not url.startswith(api + "/"):
            raise Permanent(f"refusing a pagination link outside {api}")
    LOG.warning("stopped after %d pages of %s", MAX_PAGES, path)


class GitLabEnrich(QueueWorker[RawEvent]):
    channel = "raw_events"
    model = RawEvent
    connections_per_job = 2  # the claim's transaction, and the rate_limits row beside it

    def __init__(self, db: Database, credentials: Credentials, *, exchange: http.Exchange = http.urllib_exchange,
                 concurrency: int = 4, batch: int = 25, poll_seconds: float = 30.0) -> None:
        super().__init__(db, name="enrich:gitlab", concurrency=concurrency, batch=batch, poll_seconds=poll_seconds)
        self.credentials = credentials
        self.exchange = exchange
        self.limits = SharedRateLimit(SOURCE, LIMITS, lambda: self.sessions)

    async def start(self) -> None:
        """Connect GitLab's tokens are sealed with the api ring: without it, fetching rows never could run."""
        if self.credentials.ring("api") is None:
            raise ConfigError(f"{ENV['api']} is not set")
        await super().start()

    def pending(self) -> Select:
        return (select(RawEvent)
                .where(RawEvent.source == SOURCE, RawEvent.enriched_at.is_(None), RawEvent.tenant.is_not(None))
                .order_by(RawEvent.occurred_at, RawEvent.id))

    async def process(self, session: AsyncSession, raw: RawEvent) -> None:
        fetched: dict = {}
        call = fetch_path(raw.envelope["body"])
        if call is not None:
            read_at = await session.scalar(select(func.clock_timestamp()))
            try:
                fetched = await self.fetch(raw, *call)
            except TokenRefused as error:
                # Its own transaction: the raise rolls back the row's savepoint, and the row is retried
                # (as not connected) until an owner reconnects the group
                async with self.sessions.begin() as side:
                    if await disconnect(side, SOURCE, raw.account_id, stored_before=read_at):
                        LOG.error("%s; disconnected GitLab group %s", error, raw.account_id)
                raise
        values = [{**row, "source": SOURCE, "tenant": raw.tenant, "occurred_at": raw.occurred_at,
                   "raw_event_id": raw.id} for row in to_records(raw, fetched)]
        for start in range(0, len(values), INSERT_CHUNK):
            statement = insert(SourceRecord).values(values[start : start + INSERT_CHUNK])
            await session.execute(statement.on_conflict_do_nothing(
                index_elements=["source", "tenant", "object_id", "version"]))
        raw.enriched_at = func.clock_timestamp()

    async def fetch(self, raw: RawEvent, shape: str, path: str) -> dict:
        if not raw.account_id:
            raise Permanent("a GitLab delivery with no account needs an API call")
        credential = await self.credentials.open(SOURCE, raw.account_id, "api", tenant=raw.tenant)
        if credential is None:
            raise Permanent(f"GitLab group {raw.account_id} isn't connected to tenant {raw.tenant},"
                            " and this event needs an API call")
        api, token = api_root(raw.account_id, credential)
        if shape == "object":
            body, _ = await get(self.limits, self.exchange, raw.tenant, raw.account_id, token, api + path)
            return {"compare": body.get("commits") or []}
        diffs: list = []
        async for page in pages(self.limits, self.exchange, raw.tenant, raw.account_id, token, api, path):
            diffs += page
        return {"diffs": diffs}
