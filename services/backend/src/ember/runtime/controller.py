"""Builds every component from settings, runs them under the supervisor, and handles shutdown.

Startup:

1. The process settings are loaded by the caller; without DATABASE_URL it never gets here.
2. `Deps` is built. It connects to nothing, so a Postgres outage at boot delays components, not the process.
3. Each connector loads its own settings and builds its parts. A part whose settings are missing or
   unusable is disabled with the reason, and every other part still runs.
4. EMBER_COMPONENTS picks which of the rest this process runs.
5. The supervisor starts the HTTP server and every component, each in its own task.

Shutdown, on SIGTERM or SIGINT: /health turns 503, the HTTP server stops accepting and finishes the
requests in hand, and every component stops claiming and finishes its jobs, all within
EMBER_SHUTDOWN_GRACE seconds. A second signal cancels whatever is still running. The process
then exits 0.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Gauge

from ember.connectors.base import Connector
from ember.core.errors import ConfigError
from ember.core.settings import Settings
from ember.intake.app import Intake
from ember.runtime.component import Component
from ember.runtime.deps import Deps
from ember.runtime.http import HttpServer
from ember.runtime.supervisor import Supervisor

LOG = logging.getLogger("ember.controller")
NOT_SELECTED = "not selected by EMBER_COMPONENTS"


@dataclass
class Plan:
    components: list[Component] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)  # part name: why it is not running


def plan(deps: Deps, connectors: Sequence[type[Connector]]) -> Plan:
    """What this process runs, and why each other part does not."""
    settings = deps.settings
    result = Plan()
    adapters = {}
    for connector_class in connectors:
        source = connector_class.name
        try:
            connector = connector_class.create(deps)
        except ConfigError as error:
            result.disabled[source] = str(error)
            continue
        if settings.selects("intake"):
            try:
                adapters[source] = connector.webhook()
            except ConfigError as error:
                result.disabled[f"webhook:{source}"] = str(error)
        try:
            parts = connector.components()
        except ConfigError as error:
            result.disabled[f"workers:{source}"] = str(error)
            parts = []
        for part in parts:
            if settings.selects(part.name):
                result.components.append(part)
            else:
                result.disabled[part.name] = NOT_SELECTED

    if not settings.selects("intake"):
        result.disabled["intake"] = NOT_SELECTED
    elif not adapters:
        result.disabled["intake"] = "no source has a usable webhook secret"
    else:
        result.components.insert(0, Intake(deps.db, adapters, deps.metrics))
    return result


class LoopMonitor(Component):
    """Measures how late the event loop wakes. A component that blocks the loop delays every other one."""

    name = "loop"
    INTERVAL = 1.0

    def __init__(self, deps: Deps) -> None:
        self.lag = Gauge("ember_event_loop_lag_seconds", "How late the event loop woke, last second",
                         registry=deps.metrics)
        self.worst = 0.0

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            before = time.monotonic()
            await asyncio.sleep(self.INTERVAL)
            lag = max(0.0, time.monotonic() - before - self.INTERVAL)
            self.lag.set(lag)
            self.worst = max(self.worst, lag)
            if lag > 0.5:
                LOG.warning("event loop woke %.1fs late: a component is blocking it", lag)

    def status(self) -> dict[str, Any]:
        return {"worst_lag_seconds": round(self.worst, 3)}


class Controller:
    def __init__(self, settings: Settings, connectors: Sequence[type[Connector]]) -> None:
        self.settings = settings
        self.connectors = connectors
        self.stopping = asyncio.Event()
        self._main: asyncio.Task | None = None

    def stop(self, signum: int | None = None) -> None:
        if self.stopping.is_set():  # the second signal: stop waiting for components to finish
            LOG.warning("second %s: cancelling what is still running", signal.Signals(signum).name if signum else "stop")
            if self._main is not None:
                self._main.cancel()
            return
        LOG.info("%s: shutting down", signal.Signals(signum).name if signum else "stop")
        self.stopping.set()

    async def run(self) -> int:
        self._main = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, self.stop, signum)
        deps = Deps.build(self.settings)
        try:
            built = plan(deps, self.connectors)
            for name, reason in built.disabled.items():
                LOG.warning("%s not running: %s", name, reason)
            supervisor: Supervisor
            http = HttpServer(
                deps, [route for part in built.components for route in part.routes()],
                lambda: {"components": supervisor.status(), "disabled": built.disabled},
                port=self.settings.listen_port,
            )
            supervisor = Supervisor([http, LoopMonitor(deps), *built.components], deps.metrics,
                                    restart_max=self.settings.ember_restart_max,
                                    grace=self.settings.ember_shutdown_grace)
            LOG.info("running %s", ", ".join(supervisor.units))
            try:
                await supervisor.run(self.stopping)
            except asyncio.CancelledError:
                LOG.warning("stopped before every component finished")
        finally:
            await deps.close()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)
        LOG.info("stopped")
        return 0
