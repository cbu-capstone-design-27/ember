"""Jira Cloud (docs/ingestion/jira-webhooks.md)."""

from __future__ import annotations

from pydantic import Field, SecretStr

from ember.connectors.base import Connector
from ember.connectors.jira.atlassian import OAuthClient
from ember.connectors.jira.enrich import JiraEnrich
from ember.connectors.jira.keep_alive import JiraKeepAlive
from ember.connectors.jira.webhook import JiraAdapter
from ember.core.settings import ConnectorSettings, secret
from ember.runtime.component import Component
from source_credentials import ENV


class JiraSettings(ConnectorSettings):
    jira_webhook_secret: SecretStr | None = None       # admin webhooks
    # The Atlassian 3LO app of Connect Jira (EMBER-131). The secret also verifies dynamic webhooks; with
    # the id, it refreshes the sites' tokens for enrich and the keep-alive
    jira_oauth_client_id: str | None = None
    jira_oauth_client_secret: SecretStr | None = None
    jira_enrich_concurrency: int = Field(4, ge=1, le=32)
    jira_enrich_batch: int = Field(10, ge=1, le=500)


class JiraConnector(Connector):
    name = "jira"
    Settings = JiraSettings
    settings: JiraSettings

    def adapter(self) -> JiraAdapter:  # either secret or both; the adapter refuses neither
        return JiraAdapter(secret(self.settings.jira_webhook_secret), secret(self.settings.jira_oauth_client_secret))

    def components(self) -> list[Component]:
        """A ring that is set but unreadable is a ConfigError for both. Without the ring or the client, enrich
        sets aside the rows that need a call and the keep-alive is disabled, each saying which is missing."""
        ring = self.deps.credentials.ring("api")
        client_id = (self.settings.jira_oauth_client_id or "").strip()
        client_secret = secret(self.settings.jira_oauth_client_secret).strip()
        unset = [name for name, value in ((ENV["api"], ring), ("JIRA_OAUTH_CLIENT_ID", client_id),
                                          ("JIRA_OAUTH_CLIENT_SECRET", client_secret)) if not value]
        missing = f"{', '.join(unset)} {'is' if len(unset) == 1 else 'are'} not set" if unset else ""
        client = OAuthClient(client_id, client_secret) if client_id and client_secret else None
        return [
            JiraEnrich(self.deps.db, ring=ring, client=client, missing=missing,
                       concurrency=self.settings.jira_enrich_concurrency, batch=self.settings.jira_enrich_batch),
            JiraKeepAlive(self.deps.db, ring=ring, client=client, missing=missing),
        ]
