"""GitLab (docs/ingestion/gitlab-webhooks.md)."""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field, SecretStr

from ember.connectors.base import Connector
from ember.connectors.gitlab.backfill import GitLabBackfill
from ember.connectors.gitlab.enrich import GitLabEnrich
from ember.connectors.gitlab.webhook import GitLabAdapter
from ember.core.credentials import StoredSigningTokens
from ember.core.settings import ConnectorSettings, secret
from ember.runtime.component import Component


class GitLabSettings(ConnectorSettings):
    # Shared tokens, for webhooks set up by hand. Connect GitLab stores each account's own (EMBER-149)
    gitlab_webhook_signing_token: SecretStr | None = None
    gitlab_webhook_secret_token: SecretStr | None = None
    gitlab_enrich_concurrency: int = Field(4, ge=1, le=32)
    gitlab_enrich_batch: int = Field(25, ge=1, le=500)
    gitlab_backfill_every_hours: float = Field(1.0, gt=0)


class GitLabConnector(Connector):
    name = "gitlab"
    Settings = GitLabSettings
    settings: GitLabSettings

    def adapter(self) -> GitLabAdapter:
        # The stored tokens are used whenever the webhook key ring is set; an unreadable ring disables the adapter
        stored = StoredSigningTokens(self.deps.credentials) if self.deps.credentials.ring("webhook") else None
        return GitLabAdapter(secret(self.settings.gitlab_webhook_signing_token),
                             secret(self.settings.gitlab_webhook_secret_token), tokens=stored)

    def components(self) -> list[Component]:
        return [GitLabEnrich(self.deps.db, self.deps.credentials, concurrency=self.settings.gitlab_enrich_concurrency,
                             batch=self.settings.gitlab_enrich_batch),
                GitLabBackfill(self.deps.db, self.deps.credentials,
                               every=timedelta(hours=self.settings.gitlab_backfill_every_hours))]
