"""intake (EMBER-62): the one listener for every source's webhooks, at POST /webhook/{source}.

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
        GITHUB_WEBHOOK_SECRET=... python3 intake.py

docs/ingestion/architecture.md, "The parent listener (intake)", is the design. The source's adapter
answers a handshake or verifies the delivery. Intake then stores it as it arrived in raw_events,
with `on conflict (source, delivery_id) do nothing` so a re-sent delivery changes nothing, and
answers 200 once that commits. It never calls a source API. The raw_events trigger (EMBER-75)
wakes the enrich workers on every insert, so intake sends no NOTIFY of its own.

GET /health checks Postgres and is the readiness probe. GET /metrics counts every delivery by source
and result. Logs never carry a payload. SIGINT or SIGTERM stops accepting connections and lets
requests in flight finish, then the process exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import traceback
from collections.abc import Mapping

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from sqlalchemy import exc, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from adapters import BadRequest, Delivery, SourceAdapter, Unauthorized
from adapters.github import GitHubAdapter
from models import Installation, RawEvent

LOG = logging.getLogger("intake")
MAX_BODY_BYTES = 8 * 1024 * 1024  # nginx's client_max_body_size is the same
BODIES_IN_MEMORY = 8  # deliveries handled at once; the rest wait unread, so bodies stay under 64 MiB
DB_TIMEOUT = 5  # seconds per connect, pool checkout or statement
STORE_DEADLINE = 8  # seconds for the whole store, inside GitHub's 10
GRACEFUL_SHUTDOWN = 20  # seconds for requests in flight after SIGTERM
# SQLSTATE classes meaning Postgres can't take the row now: connection exception, insufficient
# resources, operator intervention (shutdown, restart, cancel)
UNAVAILABLE_SQLSTATES = ("08", "53", "57")
DATA_EXCEPTION = "22"  # jsonb can't hold the payload (\u0000, a lone surrogate): it never will


def create_engine(database_url: str, timeout: float = DB_TIMEOUT) -> AsyncEngine:
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
        url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
    return create_async_engine(
        url, pool_pre_ping=True, pool_timeout=timeout,
        hide_parameters=True,  # an error message must not carry the payload into the logs
        connect_args={"timeout": timeout, "command_timeout": timeout},
    )


def sqlstate(error: BaseException) -> str:
    """Postgres's error code, raised by asyncpg directly (on connect) or wrapped by SQLAlchemy."""
    if isinstance(error, exc.DBAPIError):
        error = error.orig
    return getattr(error, "sqlstate", None) or ""


def failure(error: Exception) -> str:
    """Why a store failed: unavailable (nothing stored; the source may retry), unstorable or error."""
    if isinstance(error, (OSError, exc.TimeoutError)):  # refused, dropped, timed out (TimeoutError is an OSError)
        return "unavailable"
    if isinstance(error, exc.DBAPIError) and error.connection_invalidated:
        return "unavailable"
    code = sqlstate(error)
    if code.startswith(UNAVAILABLE_SQLSTATES):
        return "unavailable"
    if code.startswith(DATA_EXCEPTION):
        return "unstorable"
    return "error"


async def read_body(request: Request) -> bytes | None:
    """The whole body, or None once it is over MAX_BODY_BYTES, declared or sent."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


class Intake:
    def __init__(self, engine: AsyncEngine, adapters: Mapping[str, SourceAdapter], registry: CollectorRegistry,
                 deadline: float) -> None:
        self.engine = engine
        self.adapters = dict(adapters)
        self.registry = registry
        self.deadline = deadline
        self.in_memory = asyncio.Semaphore(BODIES_IN_MEMORY)
        self.deliveries = Counter(
            "intake_deliveries", "Webhook deliveries by source and result", ("source", "result"), registry=registry)

    def answer(self, source: str, result: str, code: int, **body: str) -> JSONResponse:
        self.deliveries.labels(source, result).inc()
        return JSONResponse(body, status_code=code)

    async def webhook(self, request: Request) -> Response:
        source = request.path_params["source"]
        adapter = self.adapters.get(source)
        if adapter is None:
            # Counted as "unknown": a made-up path must not become a new metric series
            return self.answer("unknown", "unknown_source", 404, error="unknown source")
        async with self.in_memory:
            try:
                return await self.receive(source, adapter, request)
            except Exception as error:  # a bug: counted, and logged by type and frames, never by message
                LOG.error("%s delivery failed: %s %s\n%s", source, type(error).__name__, sqlstate(error),
                          "".join(traceback.format_tb(error.__traceback__)))
                return self.answer(source, "error", 500, error="internal error")

    async def receive(self, source: str, adapter: SourceAdapter, request: Request) -> Response:
        try:
            body = await read_body(request)
        except ClientDisconnect:
            return self.answer(source, "disconnected", 400, error="client disconnected")
        if body is None:
            return self.answer(source, "too_large", 413, error="body too large")

        delivery = Delivery(request.headers, request.query_params, body)
        try:
            handshake = adapter.handshake(delivery)
            if handshake is not None:
                self.deliveries.labels(source, "handshake").inc()
                return handshake
            adapter.verify(delivery)
            row = {
                "source": source,
                "delivery_id": adapter.delivery_id(delivery),
                "account_id": adapter.account_id(delivery),
                "event_name": adapter.event_name(delivery),
                "origin": "webhook",
                "envelope": {"type": source, "body": delivery.json},
            }
            occurred_at = adapter.occurred_at(delivery)
        except Unauthorized as error:
            return self.answer(source, "unauthorized", 401, error=str(error))
        except BadRequest as error:
            return self.answer(source, "bad_request", 400, error=str(error))
        if occurred_at is not None:
            row["occurred_at"] = occurred_at  # left out, the column default makes it received_at

        try:
            async with asyncio.timeout(self.deadline):
                stored = await self.store(row)
        except Exception as error:
            result = failure(error)
            if result == "error":
                raise
            LOG.warning("%s delivery=%s %s: %s %s", source, row["delivery_id"], result,
                        type(error).__name__, sqlstate(error))
            if result == "unstorable":
                return self.answer(source, result, 400, error="postgres cannot store this payload")
            return self.answer(source, result, 503, error="postgres unavailable")
        result = "stored" if stored else "duplicate"
        LOG.info("%s %s delivery=%s event=%s account=%s",
                 source, result, row["delivery_id"], row["event_name"], row["account_id"])
        return self.answer(source, result, 200, status=result)

    async def store(self, row: dict) -> bool:
        """Insert the row unless its (source, delivery_id) is stored already. True if it was inserted."""
        tenant = select(Installation.tenant).where(
            Installation.source == row["source"], Installation.account_id == row["account_id"]
        ).scalar_subquery()  # null until the account is mapped
        statement = (
            insert(RawEvent).values(**row, tenant=tenant)
            .on_conflict_do_nothing(index_elements=["source", "delivery_id"])
            .returning(RawEvent.id)
        )
        async with self.engine.begin() as conn:
            return (await conn.execute(statement)).first() is not None

    async def health(self, request: Request) -> Response:
        try:
            async with asyncio.timeout(self.deadline), self.engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception:
            return JSONResponse({"status": "postgres unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def metrics(self, request: Request) -> Response:
        return Response(generate_latest(self.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})


def create_app(engine: AsyncEngine, adapters: Mapping[str, SourceAdapter], registry: CollectorRegistry | None = None,
               *, deadline: float = STORE_DEADLINE) -> Starlette:
    intake = Intake(engine, adapters, registry or CollectorRegistry(), deadline)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await engine.dispose()

    return Starlette(
        routes=[
            Route("/webhook/{source}", intake.webhook, methods=["POST"]),
            Route("/health", intake.health, methods=["GET"]),
            Route("/metrics", intake.metrics, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Set DATABASE_URL, e.g. postgresql://ember:...@localhost:5432/ember?sslmode=disable", file=sys.stderr)
        return 2
    try:
        adapters = [GitHubAdapter(os.environ.get("GITHUB_WEBHOOK_SECRET", ""))]
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    app = create_app(create_engine(database_url), {adapter.name: adapter for adapter in adapters})
    # Not INTAKE_PORT: Kubernetes sets that name for a Service called intake
    port = int(os.environ.get("LISTEN_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, log_config=None,
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
