"""Runs every component in its own task and restarts one that crashes, without touching the others."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge

from ember.core.errors import ConfigError
from ember.runtime.component import Component

LOG = logging.getLogger("ember.supervisor")


class State(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"    # crashed; restarting after a delay
    DISABLED = "disabled"  # ConfigError: never restarted
    STOPPED = "stopped"


@dataclass
class Unit:
    component: Component
    state: State = State.STARTING
    reason: str | None = None  # the last crash, or why it is disabled; a type and message, never a payload
    crashes: int = 0           # in a row; reset once it has run for `healthy_after`
    restarts: int = 0
    since: float = field(default_factory=time.monotonic)

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.state.value, "restarts": self.restarts}
        if self.reason:
            status["reason"] = self.reason
        return status | self.component.status()


class Supervisor:
    """Restart policy: wait 1 s, 2 s, 4 s and so on, up to `restart_max`, between attempts.

    It never gives up on a component that keeps crashing: a source API or Postgres can be down for
    hours (the 2026-11-10 outage), and the component should come back on its own when they do. The
    delay starts again from 1 s once a component has run for `healthy_after` seconds.

    On shutdown the components marked `stops_first` see `stopping` first; the rest see it once those
    have returned. All of them share `grace` seconds; one still running then is cancelled.
    """

    def __init__(self, components: Iterable[Component], registry: CollectorRegistry, *,
                 restart_max: float = 60.0, healthy_after: float = 300.0, grace: float = 50.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.units = {component.name: Unit(component) for component in components}
        self.restart_max = restart_max
        self.healthy_after = healthy_after
        self.grace = grace
        self.clock = clock
        self._up = Gauge("ember_component_up", "1 while the component is running", ("component",),
                         registry=registry)
        self._restarts = Counter("ember_component_restarts", "Restarts after a crash", ("component",),
                                 registry=registry)

    def state(self, name: str) -> State:
        return self.units[name].state

    def status(self) -> dict[str, dict[str, Any]]:
        return {name: unit.status() for name, unit in self.units.items()}

    async def run(self, stopping: asyncio.Event) -> None:
        then = asyncio.Event()  # what the components that don't stop first see
        first = [asyncio.create_task(self._keep(unit, stopping), name=name)
                 for name, unit in self.units.items() if unit.component.stops_first]
        rest = [asyncio.create_task(self._keep(unit, then), name=name)
                for name, unit in self.units.items() if not unit.component.stops_first]
        tasks = first + rest
        try:
            await stopping.wait()
            deadline = asyncio.get_running_loop().time() + self.grace
            await self._finish(first, deadline)
            then.set()
            await self._finish(rest, deadline)
        finally:
            for task in tasks:  # the supervisor itself was cancelled
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _finish(self, tasks: list[asyncio.Task], deadline: float) -> None:
        if not tasks:
            return
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            LOG.warning("%s did not stop within %.0fs; cancelling it", task.get_name(), self.grace)
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _set(self, unit: Unit, state: State, reason: str | None = None) -> None:
        unit.state, unit.since = state, self.clock()
        if reason is not None or state in (State.RUNNING, State.STOPPED):
            unit.reason = reason
        self._up.labels(unit.component.name).set(1 if state is State.RUNNING else 0)

    async def _keep(self, unit: Unit, stopping: asyncio.Event) -> None:
        component = unit.component
        while not stopping.is_set():
            self._set(unit, State.STARTING)
            started = self.clock()
            try:
                try:
                    await component.start()
                    self._set(unit, State.RUNNING)
                    await component.run(stopping)
                finally:
                    await self._close(component)
            except ConfigError as error:
                self._set(unit, State.DISABLED, str(error))
                LOG.error("%s disabled: %s", component.name, error)
                return
            except Exception as error:
                reason = f"{type(error).__name__}: {error}"
                LOG.error("%s crashed: %s", component.name, reason, exc_info=error)
            else:
                if stopping.is_set():
                    break
                reason = "returned before shutdown"
                LOG.error("%s %s", component.name, reason)

            if self.clock() - started >= self.healthy_after:
                unit.crashes = 0
            unit.crashes += 1
            unit.restarts += 1
            self._restarts.labels(component.name).inc()
            delay = min(2.0 ** (unit.crashes - 1), self.restart_max)
            self._set(unit, State.BACKOFF, reason)
            LOG.info("%s restarting in %.0fs", component.name, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), delay)
        self._set(unit, State.STOPPED)

    @staticmethod
    async def _close(component: Component) -> None:
        try:
            await component.close()
        except Exception as error:  # a failed close must not hide why the component stopped
            LOG.error("%s close failed: %s: %s", component.name, type(error).__name__, error)
