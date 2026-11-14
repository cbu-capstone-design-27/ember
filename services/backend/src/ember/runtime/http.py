"""The one HTTP listener: the routes components contribute, plus /health, /status and /metrics."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ember.runtime.component import Component
from ember.runtime.deps import Deps

LOG = logging.getLogger("ember.http")
HEALTH_TIMEOUT = 5.0
GRACEFUL_SHUTDOWN = 20.0  # seconds for requests in flight once stopping


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # the controller handles signals for every component
        yield


class HttpServer(Component):
    """GET /health is the readiness probe: 200 while Postgres answers, 503 once shutdown starts.

    GET /status reports every component's state and why each disabled part is disabled. It holds no
    secret or payload, but it is not meant for the internet: nginx exposes only /webhook/.
    """

    name = "http"
    stops_first = True

    def __init__(self, deps: Deps, routes: list[BaseRoute], status: Callable[[], dict[str, Any]],
                 *, port: int, host: str = "0.0.0.0") -> None:
        self.deps = deps
        self.extra_routes = routes
        self.report = status
        self.port = port
        self.host = host
        self.stopping = False
        self.engine: AsyncEngine | None = None
        self.server: _Server | None = None

    def app(self) -> Starlette:
        return Starlette(routes=[
            *self.extra_routes,
            Route("/health", self.health, methods=["GET"]),
            Route("/status", self.status_page, methods=["GET"]),
            Route("/metrics", self.metrics, methods=["GET"]),
        ])

    async def start(self) -> None:
        self.stopping = False
        self.engine = self.deps.db.engine(pool_size=1, timeout=HEALTH_TIMEOUT)
        config = uvicorn.Config(self.app(), host=self.host, port=self.port, access_log=False, log_config=None,
                                lifespan="off", timeout_graceful_shutdown=GRACEFUL_SHUTDOWN)
        self.server = _Server(config)

    async def run(self, stopping: asyncio.Event) -> None:
        assert self.server is not None
        serving = asyncio.create_task(self._serve())
        waiting = asyncio.create_task(stopping.wait())
        try:
            await asyncio.wait({serving, waiting}, return_when=asyncio.FIRST_COMPLETED)
            if serving.done():
                serving.result()  # raises why it stopped
                raise RuntimeError("the HTTP server stopped serving")
            self.stopping = True
            self.server.should_exit = True  # stop accepting; requests in flight finish
            await serving
        finally:
            waiting.cancel()
            serving.cancel()  # cancelled by the supervisor after the grace period

    async def _serve(self) -> None:
        try:
            await self.server.serve()
        except SystemExit:  # uvicorn exits when it can't bind; it has logged why
            raise RuntimeError(f"could not listen on port {self.port}") from None

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None

    async def health(self, request: Request) -> Response:
        if self.stopping:
            return JSONResponse({"status": "stopping"}, status_code=503)
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT), self.engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception:
            return JSONResponse({"status": "postgres unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def status_page(self, request: Request) -> Response:
        return JSONResponse({"status": "stopping" if self.stopping else "ok", **self.report()})

    async def metrics(self, request: Request) -> Response:
        return Response(generate_latest(self.deps.metrics), headers={"Content-Type": CONTENT_TYPE_LATEST})
