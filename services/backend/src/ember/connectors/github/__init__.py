"""GitHub App (docs/ingestion/github-webhooks.md)."""

from __future__ import annotations

from pydantic import SecretStr

from ember.connectors.base import Connector
from ember.connectors.github.webhook import GitHubAdapter
from ember.core.settings import ConnectorSettings, require


class GitHubSettings(ConnectorSettings):
    github_webhook_secret: SecretStr | None = None


class GitHubConnector(Connector):
    name = "github"
    Settings = GitHubSettings
    settings: GitHubSettings

    def adapter(self) -> GitHubAdapter:
        return GitHubAdapter(require(self.settings.github_webhook_secret, "GITHUB_WEBHOOK_SECRET"))
