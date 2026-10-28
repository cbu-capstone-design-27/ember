"""Slack rate-limit gates with their state in Postgres table rate_limits (EMBER-67).

pull_test.SlackClient sends every Web API call through a gate (EMBER-52):
- wait() before each call paces it at the method's tier rate.
- extend(seconds) pushes the gate back on a 429, a "ratelimited" body or a transient error.

RateLimitGate keeps that state in one process. These gates have the same interface but keep it
in rate_limits, one row per (source, tenant, bucket) with bucket = the Web API method, so every
worker process and replica spends one workspace's quota together (docs/ingestion/architecture.md,
RateLimiter). SlackClient's retry, throttle and backoff rules are unchanged.

The gates are synchronous because SlackClient is: the enrich worker runs it in a thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

SOURCE = "slack"

# Takes the next free slot and moves next_allowed_at one interval past it, in one statement, so
# two callers never get the same slot. Returns how long to wait for the slot, in seconds.
RESERVE = text(
    """
    insert into rate_limits as r (source, tenant, bucket, next_allowed_at)
    values (:source, :tenant, :bucket, now() + make_interval(secs => cast(:interval as double precision)))
    on conflict (source, tenant, bucket) do update
       set next_allowed_at = greatest(r.next_allowed_at, now())
                             + make_interval(secs => cast(:interval as double precision))
    returning extract(epoch from r.next_allowed_at
                      - make_interval(secs => cast(:interval as double precision)) - now())
    """
)

# Nobody calls before now + seconds. A later deadline already stored is kept.
EXTEND = text(
    """
    insert into rate_limits as r (source, tenant, bucket, next_allowed_at)
    values (:source, :tenant, :bucket, now() + make_interval(secs => cast(:seconds as double precision)))
    on conflict (source, tenant, bucket) do update
       set next_allowed_at = greatest(r.next_allowed_at,
                                      now() + make_interval(secs => cast(:seconds as double precision)))
    """
)


class PgRateGate:
    """One method's gate for one workspace. ``interval`` is the spacing between calls (0: none)."""

    def __init__(
        self,
        engine: Engine,
        tenant: str,
        bucket: str,
        interval: float = 0.0,
        *,
        source: str = SOURCE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._key = {"source": source, "tenant": tenant, "bucket": bucket}
        self._interval = max(0.0, float(interval))
        self._sleep = sleep

    def wait(self) -> None:
        with self._engine.begin() as conn:
            delay = conn.execute(RESERVE, {**self._key, "interval": self._interval}).scalar_one()
        delay = float(delay)
        if delay > 0:
            self._sleep(delay)

    def extend(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._engine.begin() as conn:
            conn.execute(EXTEND, {**self._key, "seconds": float(seconds)})


class PgMethodGates:
    """MethodGates for one workspace, backed by rate_limits. ``rates`` is calls per minute per method.

    Methods not in ``rates`` are not paced, but a throttle on them still holds every caller.
    """

    def __init__(
        self,
        engine: Engine,
        tenant: str,
        rates: dict[str, float] | None = None,
        *,
        source: str = SOURCE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._tenant = tenant
        self._rates = dict(rates or {})
        self._source = source
        self._sleep = sleep
        self._lock = threading.Lock()
        self._gates: dict[str, PgRateGate] = {}

    def __getitem__(self, api_method: str) -> PgRateGate:
        with self._lock:
            gate = self._gates.get(api_method)
            if gate is None:
                rate = self._rates.get(api_method)
                interval = 60.0 / rate if rate else 0.0
                gate = PgRateGate(
                    self._engine, self._tenant, api_method, interval, source=self._source, sleep=self._sleep
                )
                self._gates[api_method] = gate
            return gate
