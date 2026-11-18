"""Slack Events API (docs/ingestion/slack-webhooks.md)."""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field, SecretStr

from ember.connectors.base import Connector
from ember.connectors.slack.backfill import SlackBackfill
from ember.connectors.slack.enrich import SlackEnrich
from ember.connectors.slack.webhook import SlackAdapter
from ember.core.settings import ConnectorSettings, require
from ember.runtime.component import Component


class SlackSettings(ConnectorSettings):
    slack_signing_secret: SecretStr | None = None
    slack_enrich_concurrency: int = Field(4, ge=1, le=32)
    slack_enrich_batch: int = Field(10, ge=1, le=500)
    # How often backfill:slack catches every connected workspace up; a new connection is read within 30 s
    slack_backfill_every_hours: float = Field(1.0, gt=0)


class SlackConnector(Connector):
    name = "slack"
    Settings = SlackSettings
    settings: SlackSettings

    def adapter(self) -> SlackAdapter:
        return SlackAdapter(require(self.settings.slack_signing_secret, "SLACK_SIGNING_SECRET"))

    def components(self) -> list[Component]:
        return [SlackEnrich(self.deps.db, self.deps.credentials, concurrency=self.settings.slack_enrich_concurrency,
                            batch=self.settings.slack_enrich_batch),
                SlackBackfill(self.deps.db, self.deps.credentials,
                              every=timedelta(hours=self.settings.slack_backfill_every_hours))]
