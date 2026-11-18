"""Connect Slack's per-workspace bot tokens, for enrich:slack (EMBER-148, ADR 0010).

When an owner connects a workspace, the web app (apps/web/src/lib/slack-connect.ts) writes its
installations row and one `api` row in source_credentials, sealed with the api key ring
(packages/source-credentials), in one transaction. The sealed JSON is:

    {"bot_token": "xoxb-...", "bot_user_id": "U...", "scope": "...", "app_id": "A...",
     "installed_by": "U...", "enterprise_id": null}

Slack bot tokens don't expire (token rotation is off in slack-app-manifest.json), so unlike Jira's
there is nothing to refresh. A token is used only for the rows of the tenant that connected its
workspace, and only while that connection is live.

A workspace is disconnected here, as Disconnect in Settings does but without apps.uninstall (Slack
has already done it), when:

- Slack refuses its token during enrich (invalid_auth, token_revoked, account_inactive, ...). Only
  the token that was refused is dropped: if an owner reconnected meanwhile, the new one stays.
- app_uninstalled arrives, or tokens_revoked names our bot. Only a token stored before the event is
  dropped: a reconnect after the uninstall is newer than the event, so a late or retried delivery
  of it leaves the new connection alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from models import Installation, RawEvent, SourceCredential
from source_credentials import CredentialError, KeyRing

SOURCE = "slack"
KIND = "api"
TOKEN_PREFIXES = ("xoxb-", "xoxe.xoxb-")


class TokenUnusable(Exception):
    """The workspace's stored credential can't be opened or isn't a bot token. An operator has to
    fix the key ring (or the owner reconnect); the row waits meanwhile."""


@dataclass(frozen=True)
class WorkspaceToken:
    team_id: str
    tenant: str
    bot_token: str
    bot_user_id: str | None
    # source_credentials.updated_at as read: which token this is, so a disconnect drops only it
    updated_at: datetime

    def __repr__(self) -> str:  # never print the token
        return f"WorkspaceToken(team_id={self.team_id!r}, tenant={self.tenant!r}, updated_at={self.updated_at!r})"


def connected(team_id, tenant) -> ColumnElement[bool]:
    """True while the workspace is connected to the tenant with a stored bot token. team_id and
    tenant may be columns (the claim) or values."""
    return exists().where(
        Installation.source == SOURCE,
        Installation.account_id == team_id,
        Installation.tenant == tenant,
        Installation.disconnected_at.is_(None),
        exists().where(
            SourceCredential.source == SOURCE,
            SourceCredential.account_id == Installation.account_id,
            SourceCredential.kind == KIND,
        ),
    )


def row_connected() -> ColumnElement[bool]:
    """connected() for the raw_events row being claimed."""
    return connected(RawEvent.account_id, RawEvent.tenant)


class WorkspaceTokens:
    """Reads the workspaces' bot tokens, and disconnects a workspace whose token is gone."""

    def __init__(self, ring: KeyRing) -> None:
        if ring.kind != KIND:
            raise ValueError("Slack's bot tokens are sealed with the api key ring")
        self.ring = ring

    async def read(self, session: AsyncSession, team_id: str | None, tenant: str) -> WorkspaceToken | None:
        """The workspace's bot token for this tenant's rows, or None when it isn't connected to the
        tenant. Raises TokenUnusable when the stored credential can't be used."""
        if not team_id:
            return None
        row = (
            await session.execute(
                select(SourceCredential.key_id, SourceCredential.ciphertext, SourceCredential.updated_at)
                .join(
                    Installation,
                    and_(Installation.source == SourceCredential.source, Installation.account_id == SourceCredential.account_id),
                )
                .where(
                    SourceCredential.source == SOURCE,
                    SourceCredential.account_id == team_id,
                    SourceCredential.kind == KIND,
                    Installation.tenant == tenant,
                    Installation.disconnected_at.is_(None),
                )
            )
        ).first()
        if row is None:
            return None
        try:
            secret = self.ring.open(SOURCE, team_id, row.key_id, row.ciphertext)
        except CredentialError as exc:
            raise TokenUnusable(f"workspace {team_id}'s bot token can't be opened: {exc}") from exc
        token = secret.get("bot_token")
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIXES):
            raise TokenUnusable(f"workspace {team_id}'s stored credential holds no bot token")
        bot_user = secret.get("bot_user_id")
        return WorkspaceToken(team_id, tenant, token, bot_user if isinstance(bot_user, str) and bot_user else None, row.updated_at)

    async def bot_user(self, session: AsyncSession, team_id: str, tenant: str) -> str | None:
        """Our bot's user id in the workspace, from its stored credential, or None."""
        try:
            token = await self.read(session, team_id, tenant)
        except TokenUnusable:
            return None
        return token.bot_user_id if token else None

    async def disconnect_rejected(self, session: AsyncSession, token: WorkspaceToken) -> bool:
        """Slack refused ``token``: disconnect its workspace, unless the token stored now is another
        one (an owner reconnected meanwhile). True when it disconnected."""
        if not await self._lock_connected(session, token.team_id, token.tenant):
            return False
        current = await self._credential_updated_at(session, token.team_id)
        if current is not None and current != token.updated_at:
            return False
        await self._disconnect(session, token.team_id)
        return True

    async def disconnect_before(self, session: AsyncSession, team_id: str, tenant: str, when: datetime) -> bool:
        """Slack uninstalled the app or revoked its token at ``when``: disconnect the workspace,
        unless its token was stored at or after then (an owner reconnected since). True when it
        disconnected."""
        if not await self._lock_connected(session, team_id, tenant):
            return False
        current = await self._credential_updated_at(session, team_id)
        if current is not None and current >= when:
            return False
        await self._disconnect(session, team_id)
        return True

    async def _lock_connected(self, session: AsyncSession, team_id: str, tenant: str) -> bool:
        """Lock the workspace's live installations row for this tenant, so a reconnect committing at
        the same time is seen before deciding. False when there is none."""
        found = await session.scalar(
            select(Installation.account_id)
            .where(
                Installation.source == SOURCE,
                Installation.account_id == team_id,
                Installation.tenant == tenant,
                Installation.disconnected_at.is_(None),
            )
            .with_for_update()
        )
        return found is not None

    async def _credential_updated_at(self, session: AsyncSession, team_id: str) -> datetime | None:
        return await session.scalar(
            select(SourceCredential.updated_at).where(
                SourceCredential.source == SOURCE, SourceCredential.account_id == team_id, SourceCredential.kind == KIND
            )
        )

    async def _disconnect(self, session: AsyncSession, team_id: str) -> None:
        """What Disconnect in Settings does (ADR 0010), less apps.uninstall: the token is dead already."""
        await session.execute(
            update(Installation)
            .where(Installation.source == SOURCE, Installation.account_id == team_id, Installation.disconnected_at.is_(None))
            .values(disconnected_at=func.now())
        )
        await session.execute(
            delete(SourceCredential).where(SourceCredential.source == SOURCE, SourceCredential.account_id == team_id)
        )
