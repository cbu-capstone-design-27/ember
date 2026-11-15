"""GitHub App (docs/ingestion/github-webhooks.md)."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import Field, SecretStr

from ember.connectors.base import Connector
from ember.connectors.github import api
from ember.connectors.github.enrich import GitHubEnrich, missing_app
from ember.connectors.github.webhook import GitHubAdapter
from ember.core.errors import ConfigError
from ember.core.settings import ConnectorSettings, require
from ember.runtime.component import Component


class GitHubSettings(ConnectorSettings):
    github_webhook_secret: SecretStr | None = None
    # The App enrich calls the API as. Without it, events that need a call are set aside and the rest still go
    github_app_id: str | None = None
    github_app_private_key_path: str | None = None  # the .pem file, e.g. a mounted Secret
    github_app_private_key: SecretStr | None = None  # or the PEM itself
    github_enrich_concurrency: int = Field(4, ge=1, le=32)
    github_enrich_batch: int = Field(25, ge=1, le=500)


class GitHubConnector(Connector):
    name = "github"
    Settings = GitHubSettings
    settings: GitHubSettings

    def adapter(self) -> GitHubAdapter:
        return GitHubAdapter(require(self.settings.github_webhook_secret, "GITHUB_WEBHOOK_SECRET"))

    def components(self) -> list[Component]:
        return [GitHubEnrich(self.deps.db, app_jwt=self.app_jwt(), concurrency=self.settings.github_enrich_concurrency,
                             batch=self.settings.github_enrich_batch)]

    def app_jwt(self):
        """Mints the App's JWT. A key that is set but unusable is a ConfigError, which disables the worker."""
        app_id = (self.settings.github_app_id or "").strip()
        path = (self.settings.github_app_private_key_path or "").strip()
        pem = self.settings.github_app_private_key.get_secret_value() if self.settings.github_app_private_key else ""
        if not app_id:
            return missing_app("GITHUB_APP_ID is not set")
        if path:
            name = "GITHUB_APP_PRIVATE_KEY_PATH"
            try:
                pem = Path(path).read_text()
            except OSError as error:
                raise ConfigError(f"{name} can't be read: {type(error).__name__}") from None
        elif pem.strip():
            name = "GITHUB_APP_PRIVATE_KEY"
        else:
            return missing_app("GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY is not set")
        try:
            load_pem_private_key(pem.encode(), password=None)
        except (ValueError, TypeError):  # never echo the key
            raise ConfigError(f"{name} is not a PEM private key") from None
        return partial(api.app_jwt, app_id, pem)
