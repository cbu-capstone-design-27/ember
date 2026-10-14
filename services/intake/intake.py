"""intake (EMBER-62): the one listener for every source's webhooks, at POST /webhook/{source}.

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
        GITHUB_WEBHOOK_SECRET=... python3 intake.py

docs/ingestion/architecture.md, "The parent listener (intake)", is the design. The source's adapter
answers a handshake or verifies the delivery. Intake then stores it as it arrived in raw_events,
with `on conflict (source, delivery_id) do nothing` so a re-sent delivery changes nothing, and
answers 200 once that commits. It never calls a source API. The raw_events trigger (EMBER-75)
wakes the enrich workers on every insert, so intake sends no NOTIFY of its own.

GET /health checks Postgres and is the readiness probe. GET /metrics counts deliveries by source and
result. SIGINT or SIGTERM stops accepting connections and lets requests in flight finish, then the
process exits: 0 after Ctrl+C, and by the re-raised signal after SIGTERM, as uvicorn does.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Mapping

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from sqlalchemy import exc, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from adapters import BadRequest, Delivery, SourceAdapter, Unauthorized
from adapters.github import GitHubAdapter
from models import Installation, RawEvent

LOG = logging.getLogger("intake")
MAX_BODY_BYTES = 8 * 1024 * 1024  # nginx's client_max_body_size is the same
DB_TIMEOUT = 5  # seconds per connect or statement, well inside GitHub's 10 s
GRACEFUL_SHUTDOWN = 20  # seconds for requests in flight after SIGTERM
# Postgres refused, dropped or timed out: nothing was stored, so the source may retry
UNAVAILABLE = (OSError, exc.OperationalError, exc.InterfaceError, exc.TimeoutError)


def create_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
        url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
    return create_async_engine(
        url, pool_pre_ping=True, pool_timeout=DB_TIMEOUT,
        connect_args={"timeout": DB_TIMEOUT, "command_timeout": DB_TIMEOUT},
    )


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
    def __init__(self, engine: AsyncEngine, adapters: Mapping[str, SourceAdapter], registry: CollectorRegistry) -> None:
        self.engine = engine
        self.adapters = dict(adapters)
        self.registry = registry
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
        body = await read_body(request)
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
            stored = await self.store(row)
        except UNAVAILABLE as error:
            LOG.warning("%s delivery=%s not stored: %s", source, row["delivery_id"], error)
            return self.answer(source, "unavailable", 503, error="postgres unavailable")
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
            async with self.engine.connect() as conn:
                await conn.execute(text("select 1"))
        except UNAVAILABLE:
            return JSONResponse({"status": "postgres unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def metrics(self, request: Request) -> Response:
        return Response(generate_latest(self.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})


def create_app(engine: AsyncEngine, adapters: Mapping[str, SourceAdapter],
               registry: CollectorRegistry | None = None) -> Starlette:
    intake = Intake(engine, adapters, registry or CollectorRegistry())

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
