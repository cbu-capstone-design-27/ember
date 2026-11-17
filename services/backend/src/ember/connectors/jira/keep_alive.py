"""keep-alive:jira: keeps Connect Jira's sites working (EMBER-131), once a day at AT UTC.

For every connected site with an `api` credential (docs/ingestion/jira-webhooks.md, "Keeping dynamic
webhooks alive"):

1. Refresh the token. A refresh token unused for 90 days expires, and enrich only refreshes when it
   fetches something.
2. Extend the site's dynamic webhooks with `PUT /rest/api/3/webhook/refresh`, which moves their expiry
   to 30 days from now.

A refused refresh (invalid_grant) disconnects the site, so Settings asks for a reconnect. Any other
failure is left for tomorrow's run, well inside the 30 days; /status shows how many sites failed.

Moved from services/jira-ingestion/keep_alive.py and its CronJob (EMBER-160). Every replica runs the
schedule, and a Postgres advisory lock lets one run at a time. A replica that is down at AT skips that
day: the next day's run is still inside the 30 days.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ember.connectors.jira.atlassian import LIMITS, OAuthClient, SiteCredentials, TokenRefused, api_origin, bearer_headers
from ember.core import http
from ember.core.db import Database
from ember.core.errors import ConfigError
from ember.runtime.component import Component
from ember.work.queue_worker import wait_any
from source_credentials import KeyRing

LOG = logging.getLogger("ember.keep-alive.jira")
AT = time(10, 17)  # the old CronJob's "17 10 * * *" UTC
# shortcut: replicas whose clocks differ by more than a run's length can each run once that day.
# Harmless: a refresh locks its row, and extending a webhook twice changes nothing
LOCK = "select pg_try_advisory_lock(hashtext('ember:keep-alive:jira'))"
UNLOCK = "select pg_advisory_unlock(hashtext('ember:keep-alive:jira'))"


def next_run(now: datetime, at: time = AT) -> datetime:
    """The first `at` (UTC) after `now`."""
    today = datetime.combine(now.astimezone(timezone.utc).date(), at, tzinfo=timezone.utc)
    return today if today > now else today + timedelta(days=1)


async def keep_site(sites: SiteCredentials, site: str) -> bool:
    """Refresh one site's token and extend its webhooks. False when it should be tried again."""
    try:
        credential = await sites.fresh(site, force=True)
    except TokenRefused as error:
        await sites.disconnect(site)
        LOG.warning("%s: %s; disconnected it, so Settings asks for a reconnect", site, error)
        return True
    if credential is None:   # disconnected since the list was read
        return True
    webhook_ids = credential.get("webhook_ids") or []
    if not webhook_ids:
        LOG.warning("%s: refreshed the token, but its credential names no webhook to extend", site)
        return True
    response = await asyncio.to_thread(
        http.exchange_with_retry, sites.exchange, "PUT", f"{api_origin(credential['cloud_id'])}/rest/api/3/webhook/refresh",
        bearer_headers(credential["access_token"], json_body=True), json.dumps({"webhookIds": webhook_ids}).encode(),
        limits=LIMITS)
    if response.status >= 400:
        detail = response.body.decode("utf-8", "replace")[:300]
        LOG.error("%s: extending webhooks %s failed (%d): %s", site, webhook_ids, response.status, detail)
        return False
    try:
        expires = response.json().get("expirationDate")
    except (ValueError, AttributeError):
        expires = None
    LOG.info("%s: refreshed the token and extended webhooks %s to %s", site, webhook_ids, expires or "(not given)")
    return True


async def keep_alive(sites: SiteCredentials, stopping: asyncio.Event | None = None) -> tuple[int, int]:
    """Every connected site, one at a time, until `stopping`. (sites, failed)."""
    connected = await sites.connected_sites()
    failed = 0
    for site in connected:
        if stopping is not None and stopping.is_set():
            break
        try:
            ok = await keep_site(sites, site)
        except Exception as error:
            LOG.error("%s: failed; tomorrow's run tries again: %s: %s", site, type(error).__name__, error)
            ok = False
        failed += not ok
    return len(connected), failed


class JiraKeepAlive(Component):
    name = "keep-alive:jira"

    def __init__(self, db: Database, *, ring: KeyRing | None, client: OAuthClient | None, missing: str = "",
                 exchange: http.Exchange = http.urllib_exchange, at: time = AT,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.db = db
        self.ring = ring
        self.client = client
        self.missing = missing
        self.exchange = exchange
        self.at = at
        self.clock = clock
        self.engine: AsyncEngine | None = None
        self.sites: SiteCredentials | None = None
        self.next_at: datetime | None = None
        self.last: dict[str, Any] | None = None

    async def start(self) -> None:
        if self.ring is None or self.client is None:
            raise ConfigError(self.missing)
        self.engine = self.db.engine(pool_size=2)  # the lock's connection, and one for the sites
        self.sites = SiteCredentials(self.engine, self.ring, self.client, exchange=self.exchange)

    async def close(self) -> None:
        if self.engine is not None:
            engine, self.engine, self.sites = self.engine, None, None
            await engine.dispose()

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            self.next_at = next_run(self.clock(), self.at)
            await wait_any(stopping, timeout=(self.next_at - self.clock()).total_seconds())
            if not stopping.is_set():
                await self.run_once(stopping)

    async def run_once(self, stopping: asyncio.Event | None = None) -> bool:
        """One pass over the sites, unless another replica is running one. True when this one ran."""
        assert self.engine is not None and self.sites is not None
        async with self.engine.connect() as conn:
            if not await conn.scalar(text(LOCK)):
                LOG.info("another replica is running the keep-alive")
                return False
            try:
                sites, failed = await keep_alive(self.sites, stopping)
            finally:
                await conn.scalar(text(UNLOCK))
        self.last = {"at": self.clock().isoformat(), "sites": sites, "failed": failed}
        (LOG.error if failed else LOG.info)("keep-alive: %d site(s), %d failed", sites, failed)
        return True

    def status(self) -> dict[str, Any]:
        return {"next_run": self.next_at.isoformat() if self.next_at else None, "last_run": self.last}
