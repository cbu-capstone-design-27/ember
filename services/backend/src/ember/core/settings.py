"""Settings, read from the environment with pydantic-settings.

`Settings` is what the process itself needs. Only DATABASE_URL is required: without it nothing can run,
so the process exits. Each connector declares its own settings class (`ConnectorSettings`), loaded on
its own, so a bad value in one source's settings disables that source and nothing else.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from ember.core.errors import ConfigError


class EnvSettings(BaseSettings):
    # Field names are the variable names, lowercased. Unknown variables are ignored, so every class
    # can read the same environment
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    @classmethod
    def load(cls):
        """The settings, or ConfigError naming each bad variable (pydantic's message would echo its value)."""
        try:
            return cls()
        except ValidationError as error:
            problems = sorted({f"{str(e['loc'][0]).upper()} is {'not set' if e['type'] == 'missing' else 'invalid'}"
                               for e in error.errors() if e["loc"]})
            raise ConfigError("; ".join(problems) or "invalid settings") from None


class Settings(EnvSettings):
    database_url: SecretStr
    # Not INTAKE_PORT: Kubernetes sets that name for a Service called intake
    listen_port: int = Field(8080, ge=1, le=65535)
    # Comma-separated names or shell patterns of the components this process runs: "intake",
    # "enrich:*". The same image then runs as one container or as several
    ember_components: str = "*"
    # Seconds the components get to finish their work after SIGTERM; Kubernetes' default grace is 60
    ember_shutdown_grace: float = Field(50.0, gt=0)
    # Seconds between restarts of a crashed component double from 1 up to this
    ember_restart_max: float = Field(60.0, gt=0)

    @field_validator("database_url")
    @classmethod
    def _parses(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("empty")
        try:
            make_url(value.get_secret_value())
        except (ArgumentError, ValueError):  # a bad port is a ValueError
            raise ValueError("not a URL") from None  # load() names the variable, never the value
        return value

    def selects(self, component: str) -> bool:
        patterns = [p.strip() for p in self.ember_components.split(",") if p.strip()]
        return any(fnmatchcase(component, pattern) for pattern in patterns)


class ConnectorSettings(EnvSettings):
    """A connector's own settings. Every field is optional here; the part that needs one raises ConfigError."""


def require(value: SecretStr | str | None, name: str) -> str:
    """The value of a setting a part cannot run without, or ConfigError naming it."""
    text = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not text or not text.strip():
        raise ConfigError(f"{name} is not set")
    return text


def secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""
