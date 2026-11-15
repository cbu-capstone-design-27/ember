"""intake (EMBER-62): the one listener for every source's webhooks, at POST /webhook/{source}.

docs/ingestion/architecture.md, "The parent listener (intake)", is the design. The source's adapter
answers a handshake or verifies the delivery. Intake then stores it as it arrived in raw_events,
with `on conflict (source, delivery_id) do nothing` so a re-sent delivery changes nothing, and
answers 200 once that commits. It never calls a source API. The raw_events trigger (EMBER-75)
wakes the enrich workers on every insert, so intake sends no NOTIFY of its own.

It serves the adapter of every source whose connector is configured; any other source is unknown
(404). Metrics count every delivery by source and result. Logs never carry a payload.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
from collections.abc import Mapping
from typing import Any

from prometheus_client import CollectorRegistry, Counter
from sqlalchemy import exc, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ember.core.db import DB_TIMEOUT, Database, sqlstate
from ember.core.models import Installation, RawEvent
from ember.intake.adapter import BadRequest, Delivery, SourceAdapter, Unauthorized
from ember.runtime.component import Component

LOG = logging.getLogger("ember.intake")
MAX_BODY_BYTES = 8 * 1024 * 1024  # nginx's client_max_body_size is the same
BODIES_IN_MEMORY = 8  # deliveries handled at once; the rest wait unread, so bodies stay under 64 MiB
STORE_DEADLINE = 8  # seconds for the whole store, inside GitHub's 10
POOL_SIZE = BODIES_IN_MEMORY + 2  # a store per delivery in hand, and room for a slow one to time out
# SQLSTATE classes meaning Postgres can't take the row now: connection exception, insufficient
# resources, operator intervention (shutdown, restart, cancel)
UNAVAILABLE_SQLSTATES = ("08", "53", "57")
DATA_EXCEPTION = "22"  # jsonb can't hold the payload (\u0000, a lone surrogate): it never will


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


class Intake(Component):
    name = "intake"

    def __init__(self, db: Database, adapters: Mapping[str, SourceAdapter], registry: CollectorRegistry, *,
                 deadline: float = STORE_DEADLINE, timeout: float = DB_TIMEOUT) -> None:
        self.db = db
        self.adapters = dict(adapters)
        self.deadline = deadline
        self.timeout = timeout  # per connect, pool checkout or statement
        self.engine: AsyncEngine | None = None
        self.in_memory = asyncio.Semaphore(BODIES_IN_MEMORY)
        self.deliveries = Counter(
            "intake_deliveries", "Webhook deliveries by source and result", ("source", "result"), registry=registry)

    async def start(self) -> None:
        self.engine = self.db.engine(pool_size=POOL_SIZE, timeout=self.timeout)

    async def run(self, stopping: asyncio.Event) -> None:
        await stopping.wait()  # requests arrive through the HTTP server, which drains them on shutdown

    async def close(self) -> None:
        if self.engine is not None:
            engine, self.engine = self.engine, None
            await engine.dispose()

    def routes(self) -> list[BaseRoute]:
        return [Route("/webhook/{source}", self.webhook, methods=["POST"])]

    def status(self) -> dict[str, Any]:
        return {"sources": sorted(self.adapters)}

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
            verified = adapter.verify(delivery)
            if inspect.isawaitable(verified):  # GitLab looks up the account's own token first
                await verified
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
        if self.engine is None:  # not started yet, or restarting
            raise ConnectionError("intake has no engine")
        tenant = select(Installation.tenant).where(
            Installation.source == row["source"], Installation.account_id == row["account_id"],
            Installation.disconnected_at.is_(None),
        ).scalar_subquery()  # null until the account is connected, and again once it is disconnected
        statement = (
            insert(RawEvent).values(**row, tenant=tenant)
            .on_conflict_do_nothing(index_elements=["source", "delivery_id"])
            .returning(RawEvent.id)
        )
        async with self.engine.begin() as conn:
            return (await conn.execute(statement)).first() is not None
