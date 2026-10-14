"""enrich:gitlab (EMBER-68): turns GitLab rows in raw_events into source_records.

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
        python3 enrich.py

It claims rows as docs/ingestion/architecture.md, "Interim: claiming without the job queue",
describes: LISTEN on RAW_EVENTS_CHANNEL (default raw_events), and claim once at start, on any
notification on it (the payload is ignored) and on a 30 s poll. Each row is claimed, enriched and
marked (`enriched_at`) in its own transaction, at most CONCURRENCY (default 4) at once. What each
event becomes and what it fetches is docs/ingestion/gitlab-webhooks.md. API calls reuse pull.py's
helpers and wait on the shared `rate_limits` row for the row's account. SIGINT or SIGTERM stops
claiming, lets in-flight rows finish, and exits 0.

Needs GITLAB_API_TOKEN (read_api) only for events that fetch: merge_request open, reopen and
updates that add commits, and pushes with more commits than the 20 GitLab sends. Calls go to
GITLAB_BASE_URL (default https://gitlab.com), never to a host named in a payload.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import RateLimit, RawEvent, SourceRecord
from pull import (
    API_PREFIX,
    DEFAULT_BASE_URL,
    MAX_PAGES,
    PAGE_SIZE,
    PullError,
    Response,
    _header_float,
    _headers,
    _read_json,
    exchange_with_retry,
    parse_next_link,
    resolve_base_url,
    retry_after_seconds,
    throttle_seconds,
    urllib_exchange,
)

SOURCE = "gitlab"
CHANNEL = os.environ.get("RAW_EVENTS_CHANNEL", "raw_events")
POLL_SECONDS = 30.0
ZERO_SHA = "0" * 40  # before on a new branch, after on a deleted one
DIFF_ACTIONS = ("open", "reopen")  # plus an update that adds commits (oldrev)
APPROVALS = ("approval", "unapproval", "approved", "unapproved")
KINDS = ("merge_request", "issue", "note", "push", "project")
INSERT_CHUNK = 1000


def _time(value: str) -> datetime:
    """GitLab's two time formats: ISO 8601 (REST, and webhooks since 19.0) and `2026-01-16 05:56:25 UTC`."""
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")] + "+00:00"
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stamp(value: str | datetime) -> str:
    """A time as a version: UTC ISO 8601 with milliseconds, like GitLab's own `2026-01-16T05:56:25.000Z`.

    One version of an object gets the same string from a webhook and from REST, whatever format the
    instance sends, so unique (source, tenant, object_id, version) folds the copies together.
    """
    moment = (_time(value) if isinstance(value, str) else value).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def describe(body: dict) -> tuple[str | None, str | None]:
    """(kind, action) of a body. Webhooks carry object_kind. REST objects from reconcile and backfill
    don't, so the fields only that type has decide."""
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
    """Confidential issues and internal notes make no record, as pull.py skips them, in case a webhook
    has the confidential triggers on."""
    if body.get("event_type") in ("confidential_issue", "confidential_note"):
        return True
    attrs = _attributes(body)
    if kind == "issue":
        return attrs.get("confidential") is True
    if kind == "note":
        return attrs.get("internal") is True or attrs.get("confidential") is True
    return False


def ordering_value(raw_event: RawEvent) -> datetime:
    """The time GitLab says the event happened (gitlab-webhooks.md, "Ordering value"). Fills occurred_at."""
    # TODO(EMBER-63): read the raw_events ordering column once it lands
    kind, action = describe(raw_event.envelope["body"])
    if kind == "push" or action in APPROVALS:
        return raw_event.received_at
    attrs = _attributes(raw_event.envelope["body"])
    for field in ("updated_at", "last_activity_at"):
        if attrs.get(field):
            return _time(attrs[field])
    return raw_event.received_at


def to_records(raw_event: RawEvent, fetched: dict) -> list[dict]:
    """The source_records rows one delivery becomes: kind, object_id, version and record."""
    body = raw_event.envelope["body"]
    kind, action = describe(body)
    if kind not in KINDS or _hidden(kind, body):
        # Tag push, emoji, pipeline and the other triggers that stay off make no record
        return []
    project_id = _project_id(kind, body)
    if project_id is None:
        return []  # group-level work items (epics) reach only group webhooks
    received = stamp(raw_event.received_at)

    def row(kind: str, object_id: str, version: str, payload: dict, tombstone: bool = False, extras: dict | None = None) -> dict:
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
    object_id = {
        "merge_request": f"{project_id}!{attrs.get('iid')}",
        "issue": f"{project_id}#{attrs.get('iid')}",
        "note": f"{project_id}/note/{attrs.get('id')}",
    }[kind]
    # GitLab doesn't say approvals bump updated_at, so they version on arrival instead of folding into the last update
    version = f"{action}:{received}" if action in APPROVALS else stamp(attrs["updated_at"])
    return [row(kind, object_id, version, body, extras=fetched)]


class Worker:
    """One enrich:gitlab process. `exchange` is the GitLab boundary tests stub."""

    def __init__(
        self,
        database_url: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str = "",
        exchange=urllib_exchange,
        concurrency: int = 4,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        url = make_url(database_url).set(drivername="postgresql+asyncpg")
        if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
            url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
        # Each in-flight row holds one connection for its transaction and briefly a second for rate_limits
        self.engine = create_async_engine(url, pool_size=2 * concurrency + 1)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.api = resolve_base_url(base_url) + API_PREFIX
        self.api_token = api_token
        self.exchange = exchange
        self.poll_seconds = poll_seconds
        self.slots = asyncio.Semaphore(concurrency)
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        # ponytail: failed rows are skipped until the next poll, in memory. The job queue's retry and dead letter replace it.
        self.failed: set[int] = set()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def _notified(self, _conn, _pid, _channel, _payload: str) -> None:
        # Any notification wakes the worker. The claim filters by source, so the payload is never read.
        self.wake.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        try:
            async with self.engine.connect() as conn:
                listener = (await conn.get_raw_connection()).driver_connection
                await listener.add_listener(CHANNEL, self._notified)
                print(f"enrich:gitlab listening on {CHANNEL}", flush=True)
                while not self.stopping.is_set():
                    self.wake.clear()
                    await self._claim_until_empty()
                    try:
                        await asyncio.wait_for(self.wake.wait(), self.poll_seconds)
                    except asyncio.TimeoutError:
                        self.failed.clear()
                print(f"enrich:gitlab stopping, finishing {len(self.tasks)} in flight", flush=True)
                await asyncio.gather(*list(self.tasks))
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.engine.dispose()

    async def _claim_until_empty(self) -> None:
        """Start one job per free slot until a claim finds no row."""
        while not self.stopping.is_set():
            await self.slots.acquire()
            claimed = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._job(claimed))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            if not await claimed:
                return

    def _claim(self):
        query = (
            select(RawEvent)
            .where(RawEvent.source == SOURCE, RawEvent.enriched_at.is_(None), RawEvent.tenant.is_not(None))
            .order_by(RawEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return query.where(RawEvent.id.not_in(self.failed)) if self.failed else query

    async def _job(self, claimed: asyncio.Future) -> None:
        raw_id = None
        try:
            async with self.sessions.begin() as session:
                raw = None if self.stopping.is_set() else await session.scalar(self._claim())
                claimed.set_result(raw is not None)
                if raw is None:
                    return
                raw_id = raw.id
                count = await self.enrich(session, raw)
            print(f"raw_events {raw_id} {raw.event_name}: {count} new source_records", flush=True)
        except Exception as exc:  # noqa: BLE001 one bad row must not stop the worker
            if not claimed.done():
                claimed.set_result(False)
            if raw_id is not None:
                self.failed.add(raw_id)
            print(f"raw_events {raw_id} failed, retried after the next poll: {exc!r}", file=sys.stderr, flush=True)
        finally:
            self.slots.release()

    async def enrich(self, session: AsyncSession, raw: RawEvent) -> int:
        """Write the row's source_records and set enriched_at, in the caller's transaction. Returns rows inserted."""
        rows = to_records(raw, await self.fetch_extras(raw))
        occurred_at = ordering_value(raw)
        values = [
            {**row, "source": SOURCE, "tenant": raw.tenant, "occurred_at": occurred_at, "raw_event_id": raw.id}
            for row in rows
        ]
        inserted = 0
        for start in range(0, len(values), INSERT_CHUNK):
            statement = insert(SourceRecord).values(values[start : start + INSERT_CHUNK])
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=["source", "tenant", "object_id", "version"])
            )
            inserted += result.rowcount
        # now() is the transaction's start, i.e. the claim, before any API call. clock_timestamp() is the write.
        raw.enriched_at = func.clock_timestamp()
        return inserted

    async def fetch_extras(self, raw: RawEvent) -> dict:
        """Only what the payload lacks (gitlab-webhooks.md, "Event types"). REST objects fetch nothing."""
        body = raw.envelope["body"]
        kind, action = describe(body)
        if not body.get("object_kind") or kind not in KINDS or _hidden(kind, body):
            return {}
        project_id = _project_id(kind, body)
        if kind == "merge_request":
            attrs = body["object_attributes"]
            if action in DIFF_ACTIONS or (action == "update" and attrs.get("oldrev")):
                pages = await self.get_pages(raw, f"/projects/{project_id}/merge_requests/{attrs['iid']}/diffs")
                return {"diffs": [diff for page in pages for diff in page]}
        if kind == "push" and body["after"] != ZERO_SHA:
            if (body.get("total_commits_count") or 0) <= len(body.get("commits") or []):
                return {}
            base = body["before"]
            if base == ZERO_SHA:
                # A new branch has no base of its own, so it compares from the default branch. The first
                # push to an empty project has nothing to compare from and keeps the payload's 20.
                default = (body.get("project") or {}).get("default_branch")
                if not default or body["ref"] == f"refs/heads/{default}":
                    return {}
                base = default
            query = urlencode({"from": base, "to": body["after"]})
            compare = await self.get_object(raw, f"/projects/{project_id}/repository/compare?{query}")
            return {"compare": compare.get("commits") or []}  # diffs stay out; a push's can be huge
        return {}

    def _headers(self) -> dict[str, str]:
        if not self.api_token:
            raise PullError("Set GITLAB_API_TOKEN (read_api) for events that fetch from GitLab")
        return _headers(self.api_token)

    def _bucket(self, raw: RawEvent) -> str:
        # One read token per tenant group, so the account is the token's bucket
        body = raw.envelope["body"]
        return raw.account_id or str(_project_id(describe(body)[0], body))

    async def get_object(self, raw: RawEvent, path: str) -> dict:
        exchange = self.limited_exchange(raw.tenant, self._bucket(raw))
        url = f"{self.api}{path}"
        response = await asyncio.to_thread(exchange_with_retry, exchange, "GET", url, self._headers())
        return _read_json(response, f"GET {url}")

    async def get_pages(self, raw: RawEvent, path: str) -> list:
        exchange = self.limited_exchange(raw.tenant, self._bucket(raw))
        headers = self._headers()
        url: str | None = f"{self.api}{path}?per_page={PAGE_SIZE}"
        pages = []
        while url and len(pages) < MAX_PAGES:
            response = await asyncio.to_thread(exchange_with_retry, exchange, "GET", url, headers)
            pages.append(_read_json(response, f"GET {url}"))
            url = parse_next_link(response.headers.get("link"))
            if url is not None and not url.startswith(self.api + "/"):
                # The token only ever goes to GITLAB_BASE_URL
                raise PullError(f"refusing pagination link outside {self.api}: {url}")
        return pages

    def limited_exchange(self, tenant: str, bucket: str):
        """A synchronous exchange, run in a thread, that waits on and updates the rate_limits row."""
        loop = asyncio.get_running_loop()

        def exchange(method: str, url: str, headers: dict[str, str]) -> Response:
            asyncio.run_coroutine_threadsafe(self.wait_turn(tenant, bucket), loop).result()
            response = self.exchange(method, url, headers)
            asyncio.run_coroutine_threadsafe(self.record_limit(tenant, bucket, response), loop).result()
            return response

        return exchange

    def _limit_row(self, tenant: str, bucket: str):
        return (RateLimit.source == SOURCE, RateLimit.tenant == tenant, RateLimit.bucket == bucket)

    async def wait_turn(self, tenant: str, bucket: str) -> None:
        async with self.sessions() as session:
            delay = await session.scalar(
                select(func.extract("epoch", RateLimit.next_allowed_at - func.now())).where(*self._limit_row(tenant, bucket))
            )
        if delay is not None and delay > 0:
            await asyncio.sleep(float(delay))

    async def record_limit(self, tenant: str, bucket: str, response: Response) -> None:
        remaining = _header_float(response, "ratelimit-remaining")
        # pull.py decides the wait: a 429 waits out Retry-After (else RateLimit-Reset), anything else pauses
        # when RateLimit-Remaining is at or under its floor. 5xx backoff stays per request.
        if response.status == 429:
            wait = retry_after_seconds(response) or 0.0
        else:
            wait = throttle_seconds(response) or 0.0
        next_allowed_at = func.now() + timedelta(seconds=wait)
        statement = insert(RateLimit).values(
            source=SOURCE,
            tenant=tenant,
            bucket=bucket,
            next_allowed_at=next_allowed_at,
            remaining=None if remaining is None else int(remaining),
        )
        statement = statement.on_conflict_do_update(
            index_elements=["source", "tenant", "bucket"],
            set_={
                # Never shorten a wait another worker recorded
                "next_allowed_at": func.greatest(RateLimit.next_allowed_at, statement.excluded.next_allowed_at),
                "remaining": statement.excluded.remaining,
            },
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Set DATABASE_URL, e.g. postgresql://ember:...@localhost:5432/ember?sslmode=disable", file=sys.stderr)
        return 2
    try:
        worker = Worker(
            database_url,
            base_url=os.environ.get("GITLAB_BASE_URL", ""),
            api_token=os.environ.get("GITLAB_API_TOKEN", "").strip(),
            concurrency=int(os.environ.get("CONCURRENCY", "4")),
        )
    except PullError as exc:
        print(exc, file=sys.stderr)
        return 2
    asyncio.run(worker.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
