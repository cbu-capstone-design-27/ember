"""Keep Connect Jira's sites working (EMBER-131). Run once a day by CronJob enrich-jira-keep-alive.

    DATABASE_URL=... SOURCE_CREDENTIALS_API_KEYS=... JIRA_OAUTH_CLIENT_ID=... JIRA_OAUTH_CLIENT_SECRET=... \\
    PYTHONPATH=../../packages/source-credentials python3 keep_alive.py

For every connected site with an `api` credential (docs/ingestion/jira-webhooks.md, "Keeping dynamic
webhooks alive"):

1. Refresh the token. A refresh token unused for 90 days expires, and enrich only refreshes when it
   fetches something.
2. Extend the site's dynamic webhooks with `PUT /rest/api/3/webhook/refresh`, which moves their expiry
   to 30 days from now.

A refused refresh (invalid_grant) disconnects the site, so Settings asks for a reconnect. Any other
failure is left for tomorrow's run, well inside the 30 days, and makes the run exit 1 so the Job shows
it failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Optional

from atlassian import OAuthClient, SiteCredentials, TokenRefused, api_origin, bearer_headers
from enrich import make_engine
from pull_test import exchange_with_retry, urllib_exchange
from source_credentials import CredentialError, KeyRing

log = logging.getLogger("keep-alive:jira")


async def keep_site(credentials: SiteCredentials, site: str) -> bool:
    """Refresh one site's token and extend its webhooks. False when it should be tried again."""
    try:
        credential: Optional[dict] = await credentials.fresh(site, force=True)
    except TokenRefused as exc:
        await credentials.disconnect(site)
        log.warning("%s: %s; disconnected it, so Settings asks for a reconnect", site, exc)
        return True
    if credential is None:   # disconnected since the list was read
        return True
    webhook_ids = credential.get("webhook_ids") or []
    if not webhook_ids:
        log.warning("%s: refreshed the token, but its credential names no webhook to extend", site)
        return True
    response = await asyncio.to_thread(
        exchange_with_retry, credentials.exchange, "PUT", f"{api_origin(credential['cloud_id'])}/rest/api/3/webhook/refresh",
        bearer_headers(credential["access_token"], json_body=True), json.dumps({"webhookIds": webhook_ids}).encode())
    if response.status >= 400:
        detail = response.body.decode("utf-8", "replace")[:300]
        log.error("%s: extending webhooks %s failed (%d): %s", site, webhook_ids, response.status, detail)
        return False
    try:
        expires = response.json().get("expirationDate")
    except (ValueError, AttributeError):
        expires = None
    log.info("%s: refreshed the token and extended webhooks %s to %s", site, webhook_ids, expires or "(not given)")
    return True


async def keep_alive(credentials: SiteCredentials) -> int:
    """Every connected site, one at a time. The number that failed."""
    sites = await credentials.connected_sites()
    log.info("%d connected site(s)", len(sites))
    failed = 0
    for site in sites:
        try:
            ok = await keep_site(credentials, site)
        except Exception:
            log.exception("%s: failed; tomorrow's run tries again", site)
            ok = False
        failed += not ok
    return failed


def main(exchange=urllib_exchange) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    try:
        ring = KeyRing.from_env("api")
    except CredentialError as exc:
        print(exc, file=sys.stderr)
        return 2
    client = OAuthClient.from_env()
    if not database_url or ring is None or client is None:
        print("DATABASE_URL, SOURCE_CREDENTIALS_API_KEYS, JIRA_OAUTH_CLIENT_ID and JIRA_OAUTH_CLIENT_SECRET are required",
              file=sys.stderr)
        return 2

    async def run() -> int:
        engine = make_engine(database_url, pool_size=1)
        try:
            return await keep_alive(SiteCredentials(engine, ring, client, exchange=exchange))
        finally:
            await engine.dispose()

    failed = asyncio.run(run())
    if failed:
        log.error("%d site(s) failed", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
