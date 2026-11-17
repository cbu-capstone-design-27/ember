"""Connect Jira's per-site tokens, for enrich:jira and the keep-alive (EMBER-131).

When an owner connects a site, the web app stores one `api` row in source_credentials, sealed with the
api key ring (packages/source-credentials). docs/ingestion/jira-webhooks.md, "The `api` credential",
lists its fields. Atlassian rotates refresh tokens, so a refresh locks the row, refreshes, writes the
new pair back and commits before the token is used. apps/web/src/lib/jira-connect.ts's
freshJiraCredential does the same. Moved from services/jira-ingestion/atlassian.py (EMBER-160).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ember.core import http
from ember.core.errors import Permanent, Transient
from ember.core.models import Installation, SourceCredential
from source_credentials import KeyRing

SOURCE = "jira"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API = "https://api.atlassian.com"
# Refresh when the access token has less than this left, as the web app does
REFRESH_MARGIN_SECONDS = 300
# The credential row stays locked while a refresh is throttled, so wait out only a short limit
REFRESH_THROTTLE_BUDGET = 60.0


class AtlassianLimits(http.Limits):  # the default rule: a 429 is a limit, a 403 a permission error
    name = "Atlassian"


LIMITS = AtlassianLimits()


@dataclass(frozen=True)
class OAuthClient:
    """The Atlassian 3LO app's client, which a refresh needs."""

    client_id: str
    client_secret: str


class TokenRefused(Permanent):
    """Atlassian answered invalid_grant: the user revoked the app, lost the site, or left the refresh
    token unused for 90 days. Nothing works for the site until someone reconnects it."""


def api_origin(cloud_id: str) -> str:
    """Where a 3LO token calls the site's REST API."""
    return f"{API}/ex/jira/{cloud_id}"


def issue_url(cloud_id: str, issue_id: str) -> str:
    return f"{api_origin(cloud_id)}/rest/api/3/issue/{issue_id}?{urlencode({'fields': '*all'})}"


def bearer_headers(access_token: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": http.USER_AGENT}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def refresh_tokens(client: OAuthClient, refresh_token: str, exchange: http.Exchange,
                   clock: Callable[[], float] = time.time) -> dict:
    """A new access_token, refresh_token, expires_at and scope. Retries 429, 5xx and network errors: a
    retry after a lost answer reuses the old refresh token, which works for 10 more minutes."""
    body = json.dumps({"grant_type": "refresh_token", "client_id": client.client_id,
                       "client_secret": client.client_secret, "refresh_token": refresh_token}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": http.USER_AGENT}
    response = http.exchange_with_retry(exchange, "POST", TOKEN_URL, headers, body, limits=LIMITS,
                                        throttle_budget=REFRESH_THROTTLE_BUDGET)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if response.status >= 400:
        error = payload.get("error")
        if error == "invalid_grant":
            raise TokenRefused(f"Atlassian refused the refresh token ({response.status} invalid_grant)")
        # Any other refusal (our client id or secret, say) is ours to fix: Permanent unless it's a limit or a 5xx
        raise http.failure(response, f"token refresh ({error or 'no error code'})", LIMITS)
    if not payload.get("access_token"):
        raise Transient(f"Atlassian's refresh answer ({response.status}) had no access token")
    if not payload.get("refresh_token"):
        raise Permanent("Atlassian sent no refresh token; offline_access wasn't granted")
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": int(clock()) + int(payload.get("expires_in") or 3600),
    }
    if payload.get("scope"):
        tokens["scope"] = payload["scope"]
    return tokens


class SiteCredentials:
    """Reads, refreshes and drops the sites' api credentials. `exchange` is the HTTP boundary tests replace."""

    def __init__(self, engine: AsyncEngine, ring: KeyRing, client: OAuthClient, *,
                 exchange: http.Exchange = http.urllib_exchange, clock: Callable[[], float] = time.time) -> None:
        if ring.kind != "api":
            raise ValueError("Jira's credentials are sealed with the api key ring")
        self.engine = engine
        self.ring = ring
        self.client = client
        self.exchange = exchange
        self.clock = clock

    async def fresh(self, account_id: str, *, tenant: str | None = None, force: bool = False) -> dict | None:
        """The site's credential, refreshed first when its access token is about to expire, or always
        with force. None when the site has none or is disconnected, and, with tenant, when another
        tenant connected it: a token is used only for its own tenant's rows."""
        if not force:
            async with self.engine.connect() as conn:
                credential = await self._read(conn, account_id, tenant, lock=False)
            if credential is None or not self._expiring(credential):
                return credential
        # Lock and read again: another worker may have refreshed it meanwhile
        async with self.engine.begin() as conn:
            credential = await self._read(conn, account_id, tenant, lock=True)
            if credential is None or not (force or self._expiring(credential)):
                return credential
            tokens = await asyncio.to_thread(refresh_tokens, self.client, credential["refresh_token"],
                                             self.exchange, self.clock)
            credential = {**credential, **tokens}
            sealed = self.ring.seal(SOURCE, account_id, credential)
            await conn.execute(
                update(SourceCredential)
                .where(SourceCredential.source == SOURCE, SourceCredential.account_id == account_id,
                       SourceCredential.kind == "api")
                .values(key_id=sealed.key_id, ciphertext=sealed.ciphertext, updated_at=func.now())
            )
        return credential

    def _expiring(self, credential: dict) -> bool:
        return credential["expires_at"] - REFRESH_MARGIN_SECONDS <= self.clock()

    def _connected(self):
        return (select(SourceCredential.account_id, SourceCredential.key_id, SourceCredential.ciphertext)
                .join(Installation, (Installation.source == SourceCredential.source)
                      & (Installation.account_id == SourceCredential.account_id))
                .where(SourceCredential.source == SOURCE, SourceCredential.kind == "api",
                       Installation.disconnected_at.is_(None)))

    async def _read(self, conn: AsyncConnection, account_id: str, tenant: str | None, *, lock: bool) -> dict | None:
        query = self._connected().where(SourceCredential.account_id == account_id)
        if tenant is not None:
            query = query.where(Installation.tenant == tenant)
        if lock:
            query = query.with_for_update(of=SourceCredential)
        row = (await conn.execute(query)).first()
        return None if row is None else self.ring.open(SOURCE, account_id, row.key_id, row.ciphertext)

    async def connected_sites(self) -> list[str]:
        """Every connected site with an api credential."""
        async with self.engine.connect() as conn:
            rows = await conn.execute(self._connected().order_by(SourceCredential.account_id))
            return [row.account_id for row in rows]

    async def disconnect(self, account_id: str) -> None:
        """What the web app's Disconnect does, without deleting the webhooks: no token can any more.
        Settings then shows the site as disconnected, to reconnect."""
        async with self.engine.begin() as conn:
            await conn.execute(
                update(Installation)
                .where(Installation.source == SOURCE, Installation.account_id == account_id,
                       Installation.disconnected_at.is_(None))
                .values(disconnected_at=func.now())
            )
            await conn.execute(
                delete(SourceCredential).where(SourceCredential.source == SOURCE, SourceCredential.account_id == account_id)
            )
