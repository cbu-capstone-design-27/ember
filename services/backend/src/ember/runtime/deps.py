"""The shared dependencies every component is built with, made once by the controller.

Components take what they need in their constructor (plain constructor injection; no framework).
Tests build components with their own `Deps`, or with the single dependency a component takes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from prometheus_client import CollectorRegistry

from ember.core.credentials import Credentials
from ember.core.db import Database
from ember.core.settings import Settings


@dataclass(frozen=True)
class Deps:
    settings: Settings
    db: Database
    credentials: Credentials
    metrics: CollectorRegistry

    @classmethod
    def build(cls, settings: Settings, environ: Mapping[str, str] = os.environ) -> Deps:
        """Connects to nothing: each component connects when it starts, so one failing to connect fails alone."""
        db = Database(settings.database_url.get_secret_value())
        return cls(settings=settings, db=db, credentials=Credentials(db, environ), metrics=CollectorRegistry())

    async def close(self) -> None:
        await self.credentials.close()
