"""What the controller starts, supervises and stops."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from starlette.routing import BaseRoute


class Component(ABC):
    """One long-running part of the backend: intake, an enrich worker, a scheduled job.

    The supervisor runs `start()`, then `run()`, then `close()`, and runs them again after a crash:

    - `start()` acquires what the component needs: engines, clients. Raising ConfigError disables
      the component for the life of the process; anything else is a crash, retried with backoff.
    - `run(stopping)` does the work until `stopping` is set, then finishes the work in hand and
      returns. Returning before `stopping` is set counts as a crash.
    - `close()` releases what `start()` acquired. It runs after every `start()`, even a failed one,
      so it must cope with a half-started component.

    A component never handles signals or exits the process; the controller does both.
    """

    name: str
    # Ingress, like the HTTP server: it stops first, and the rest only once it has, since what it
    # has in hand (a request) may still need them (intake's engine)
    stops_first: bool = False

    async def start(self) -> None:
        pass

    @abstractmethod
    async def run(self, stopping: asyncio.Event) -> None: ...

    async def close(self) -> None:
        pass

    def routes(self) -> list[BaseRoute]:
        """HTTP routes this component serves on the shared listener, e.g. intake's /webhook/{source}."""
        return []

    def status(self) -> dict[str, Any]:
        """Extra detail for GET /status. Never a secret or a payload."""
        return {}
