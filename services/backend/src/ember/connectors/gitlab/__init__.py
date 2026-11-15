"""GitLab (docs/ingestion/gitlab-webhooks.md)."""

from __future__ import annotations

from pydantic import SecretStr

from ember.connectors.base import Connector
from ember.connectors.gitlab.webhook import GitLabAdapter
from ember.core.credentials import StoredSigningTokens
from ember.core.settings import ConnectorSettings, secret


class GitLabSettings(ConnectorSettings):
    # Shared tokens, for webhooks set up by hand. Connect GitLab stores each account's own (EMBER-149)
    gitlab_webhook_signing_token: SecretStr | None = None
    gitlab_webhook_secret_token: SecretStr | None = None


class GitLabConnector(Connector):
    name = "gitlab"
    Settings = GitLabSettings
    settings: GitLabSettings

    def adapter(self) -> GitLabAdapter:
        # The stored tokens are used whenever the webhook key ring is set; an unreadable ring disables the adapter
        stored = StoredSigningTokens(self.deps.credentials) if self.deps.credentials.ring("webhook") else None
        return GitLabAdapter(secret(self.settings.gitlab_webhook_signing_token),
                             secret(self.settings.gitlab_webhook_secret_token), tokens=stored)
