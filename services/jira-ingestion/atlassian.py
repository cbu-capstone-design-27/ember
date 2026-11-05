"""Connect Jira's per-site tokens, for enrich:jira and the keep-alive (EMBER-131).

When an owner connects a site, the web app stores one `api` row in source_credentials, sealed with the
api key ring (packages/source-credentials). docs/ingestion/jira-webhooks.md, "The `api` credential",
lists its fields. Atlassian rotates refresh tokens, so a refresh locks the row, refreshes, writes the
new pair back and commits before the token is used. apps/web/src/lib/jira-connect.ts's
freshJiraCredential does the same.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from models import Installation, SourceCredential
from pull_test import USER_AGENT, PullError, exchange_with_retry, urllib_exchange
from source_credentials import KeyRing

SOURCE = "jira"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API = "https://api.atlassian.com"
# Refresh when the access token has less than this left, as the web app does
REFRESH_MARGIN_SECONDS = 300


@dataclass(frozen=True)
class OAuthClient:
    """The Atlassian 3LO app's client, which a refresh needs."""

    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Optional["OAuthClient"]:
        client_id = environ.get("JIRA_OAUTH_CLIENT_ID", "").strip()
        client_secret = environ.get("JIRA_OAUTH_CLIENT_SECRET", "").strip()
        return cls(client_id, client_secret) if client_id and client_secret else None


class TokenRefused(PullError):
    """Atlassian answered invalid_grant: the user revoked the app, lost the site, or left the refresh
    token unused for 90 days. Nothing works for the site until someone reconnects it."""


def api_origin(cloud_id: str) -> str:
    """Where a 3LO token calls the site's REST API."""
    return f"{API}/ex/jira/{cloud_id}"


def bearer_headers(access_token: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": USER_AGENT}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def refresh_tokens(client: OAuthClient, refresh_token: str, exchange=urllib_exchange,
                   clock: Callable[[], float] = time.time) -> dict:
    """A new access_token, refresh_token, expires_at and scope. Retries 429, 5xx and network errors: a
    retry after a lost answer reuses the old refresh token, which works for 10 more minutes."""
    body = json.dumps({"grant_type": "refresh_token", "client_id": client.client_id,
                       "client_secret": client.client_secret, "refresh_token": refresh_token}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    # The credential row stays locked meanwhile, so don't wait out a long throttle
    response = exchange_with_retry(exchange, "POST", TOKEN_URL, headers, body, throttle_budget=60.0)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if response.status >= 400 or not payload.get("access_token"):
        error = payload.get("error")
        if error == "invalid_grant":
            raise TokenRefused(f"Atlassian refused the refresh token ({response.status} invalid_grant)")
        raise PullError(f"Atlassian refused the refresh ({response.status}): {error or 'no access token'}")
    if not payload.get("refresh_token"):
        raise PullError("Atlassian sent no refresh token; offline_access wasn't granted")
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": int(clock()) + int(payload.get("expires_in") or 3600),
    }
    if payload.get("scope"):
        tokens["scope"] = payload["scope"]
    return tokens


class SiteCredentials:
    """Reads, refreshes and drops the sites' api credentials. exchange is pull_test's HTTP boundary."""

    def __init__(self, engine: AsyncEngine, ring: KeyRing, client: OAuthClient, *,
                 exchange=urllib_exchange, clock: Callable[[], float] = time.time) -> None:
        if ring.kind != "api":
            raise ValueError("Jira's credentials are sealed with the api key ring")
        self.engine = engine
        self.ring = ring
        self.client = client
        self.exchange = exchange
        self.clock = clock

    async def fresh(self, account_id: str, *, tenant: Optional[str] = None, force: bool = False) -> Optional[dict]:
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
            tokens = await asyncio.to_thread(refresh_tokens, self.client, credential["refresh_token"], self.exchange, self.clock)
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

    async def _read(self, conn, account_id: str, tenant: Optional[str], *, lock: bool) -> Optional[dict]:
        query = (
            select(SourceCredential.key_id, SourceCredential.ciphertext)
            .join(Installation, (Installation.source == SourceCredential.source)
                  & (Installation.account_id == SourceCredential.account_id))
            .where(SourceCredential.source == SOURCE, SourceCredential.account_id == account_id,
                   SourceCredential.kind == "api", Installation.disconnected_at.is_(None))
        )
        if tenant is not None:
            query = query.where(Installation.tenant == tenant)
        if lock:
            query = query.with_for_update(of=SourceCredential)
        row = (await conn.execute(query)).first()
        return None if row is None else self.ring.open(SOURCE, account_id, row.key_id, row.ciphertext)

    async def connected_sites(self) -> list[str]:
        """Every connected site with an api credential."""
        async with self.engine.connect() as conn:
            result = await conn.scalars(
                select(SourceCredential.account_id)
                .join(Installation, (Installation.source == SourceCredential.source)
                      & (Installation.account_id == SourceCredential.account_id))
                .where(SourceCredential.source == SOURCE, SourceCredential.kind == "api",
                       Installation.disconnected_at.is_(None))
                .order_by(SourceCredential.account_id)
            )
            return list(result)

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
