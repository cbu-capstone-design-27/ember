"""The failures a component or a job can raise, and what the runtime does with each.

docs/ingestion/architecture.md, "Failures and replay", classifies job failures as throttled, transient
or permanent. ConfigError is the runtime's own: the component cannot run with the settings it was
given, so it is disabled with that reason and never restarted, while every other component runs.
"""

from __future__ import annotations


class EmberError(Exception):
    pass


class ConfigError(EmberError):
    """The settings this part needs are missing or unusable. The message names the variable, never its value."""


class Throttled(EmberError):
    """The source asked us to wait. Retried after `retry_after` seconds; not counted as an attempt."""

    def __init__(self, message: str = "throttled", *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, retry_after)


class Transient(EmberError):
    """Worth retrying with backoff: a timeout, a 5xx, a dropped connection."""


class Permanent(EmberError):
    """Retrying will not help: the payload or the object is bad. The job is set aside."""
