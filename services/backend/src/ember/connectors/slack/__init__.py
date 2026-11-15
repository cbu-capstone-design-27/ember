"""Slack Events API (docs/ingestion/slack-webhooks.md)."""

from __future__ import annotations

from pydantic import SecretStr

from ember.connectors.base import Connector
from ember.connectors.slack.webhook import SlackAdapter
from ember.core.settings import ConnectorSettings, require


class SlackSettings(ConnectorSettings):
    slack_signing_secret: SecretStr | None = None


class SlackConnector(Connector):
    name = "slack"
    Settings = SlackSettings
    settings: SlackSettings

    def adapter(self) -> SlackAdapter:
        return SlackAdapter(require(self.settings.slack_signing_secret, "SLACK_SIGNING_SECRET"))
