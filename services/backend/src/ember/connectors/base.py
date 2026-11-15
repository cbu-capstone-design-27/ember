"""What every source provides. One subclass per source, in connectors/<source>/."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ember.core.errors import ConfigError
from ember.core.settings import ConnectorSettings
from ember.intake.adapter import Source, SourceAdapter
from ember.runtime.component import Component
from ember.runtime.deps import Deps


class Connector(ABC):
    """A source's parts, built from its own settings and the shared dependencies.

    Each part is built on its own, and raises ConfigError when the settings it needs are missing:
    a source whose webhook secret is set but whose API credentials are not still receives
    deliveries. The controller disables that one part and reports why on /status.

    Parts so far: `webhook()`, the adapter intake serves at /webhook/<name>, and `components()`,
    the source's workers and scheduled jobs. Later (EMBER-168) a connector also owns its Connect flow.
    """

    name: ClassVar[Source]
    Settings: ClassVar[type[ConnectorSettings]]

    def __init__(self, settings: ConnectorSettings, deps: Deps) -> None:
        self.settings = settings
        self.deps = deps

    @classmethod
    def create(cls, deps: Deps) -> Connector:
        """Load this source's settings and build the connector, or raise ConfigError naming the bad variables."""
        return cls(cls.Settings.load(), deps)

    @abstractmethod
    def adapter(self) -> SourceAdapter:
        """The webhook adapter. Adapters raise ValueError for an unusable secret."""

    def webhook(self) -> SourceAdapter:
        try:
            return self.adapter()
        except ValueError as error:  # the adapters' messages name the variable, never its value
            raise ConfigError(str(error)) from None

    def components(self) -> list[Component]:
        return []
