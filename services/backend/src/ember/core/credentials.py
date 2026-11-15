"""Opening `source_credentials` rows (ADR 0010, EMBER-147).

The format is packages/source-credentials, which the image copies in as the top-level module
`source_credentials` (its README: each consumer copies it). The web app seals rows today; EMBER-168
moves sealing here, in the same format, so no row is re-encrypted.

The two key rings stay apart, as ADR 0004 asks: `webhook` opens what intake verifies deliveries
with, `api` what enrich calls a source with. A process given only one ring can only open that kind,
so a split deployment (EMBER_COMPONENTS) keeps them in different containers.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ember.core.db import Database
from ember.core.errors import ConfigError
from ember.core.models import SourceCredential
from source_credentials import ENV, CredentialError, KeyRing


class Credentials:
    """The key rings, read once from the environment, and the rows they open.

    A ring that is set but unreadable is a ConfigError for the parts that need it only.
    """

    def __init__(self, db: Database, environ: Mapping[str, str]) -> None:
        self._engine: AsyncEngine = db.engine(pool_size=2)  # connects to nothing until the first open()
        self._rings: dict[str, KeyRing | None] = {}
        self._errors: dict[str, str] = {}
        for kind in ENV:
            try:
                self._rings[kind] = KeyRing.from_env(kind, environ)
            except CredentialError as error:  # never carries key material
                self._errors[kind] = f"{ENV[kind]} can't be read: {error}"

    def ring(self, kind: str) -> KeyRing | None:
        """That kind's ring, None when it is not set, or ConfigError when it is set but unreadable."""
        if kind in self._errors:
            raise ConfigError(self._errors[kind])
        return self._rings[kind]

    async def open(self, source: str, account_id: str, kind: str) -> dict | None:
        """The account's credential of that kind, opened, or None when it has none."""
        ring = self.ring(kind)
        if ring is None:
            raise ConfigError(f"{ENV[kind]} is not set")
        query = select(SourceCredential.key_id, SourceCredential.ciphertext).where(
            SourceCredential.source == source, SourceCredential.account_id == account_id,
            SourceCredential.kind == kind,
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return None if row is None else ring.open(source, account_id, row.key_id, row.ciphertext)

    async def close(self) -> None:
        await self._engine.dispose()


class StoredSigningTokens:
    """The GitLab signing tokens Connect GitLab stored (EMBER-149): one `webhook` row per account.

    The web app seals `{"signing_tokens": ["whsec_...", ...]}`, one token per webhook the owner
    added: GitLab generates each webhook's own. Disconnect deletes the row, so a disconnected
    account's deliveries fail verification again.
    """

    def __init__(self, credentials: Credentials, source: str = "gitlab") -> None:
        self.credentials = credentials
        self.source = source

    async def get(self, account_id: str) -> list[str]:
        opened = await self.credentials.open(self.source, account_id, "webhook")
        if opened is None:
            return []
        tokens = opened.get("signing_tokens")
        if not isinstance(tokens, list) or not all(isinstance(token, str) and token for token in tokens):
            raise ValueError(f"the {self.source} webhook credential for {account_id} has no signing_tokens list")
        return tokens
