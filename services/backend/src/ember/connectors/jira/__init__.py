"""Jira Cloud (docs/ingestion/jira-webhooks.md)."""

from __future__ import annotations

from pydantic import SecretStr

from ember.connectors.base import Connector
from ember.connectors.jira.webhook import JiraAdapter
from ember.core.settings import ConnectorSettings, secret


class JiraSettings(ConnectorSettings):
    jira_webhook_secret: SecretStr | None = None       # admin webhooks
    jira_oauth_client_secret: SecretStr | None = None  # dynamic webhooks from Connect Jira (EMBER-131)


class JiraConnector(Connector):
    name = "jira"
    Settings = JiraSettings
    settings: JiraSettings

    def adapter(self) -> JiraAdapter:  # either secret or both; the adapter refuses neither
        return JiraAdapter(secret(self.settings.jira_webhook_secret), secret(self.settings.jira_oauth_client_secret))
