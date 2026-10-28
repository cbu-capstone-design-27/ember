"""The extract and load workers (EMBER-85): source_records -> episodes -> Neo4j, through Postgres.

    DATABASE_URL=postgresql://... python -m loader extract
    DATABASE_URL=postgresql://... NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=... python -m loader load

Both claim rows like the enrich workers until the job queue lands (docs/ingestion/architecture.md,
"Interim: claiming without the job queue"): LISTEN on a channel (extract: source_records, load:
episodes; SOURCE_RECORDS_CHANNEL and EPISODES_CHANNEL override them), and claim once at start, on any
notification (the payload is ignored) and on a 30 s poll. CONCURRENCY (default 4) runners claim at
once. SIGINT or SIGTERM stops claiming, lets the jobs in flight finish, and exits 0; a second one
exits at once, and what was in flight stays pending in Postgres.

extract claims one source_records row that has no episode from this EXTRACTOR_VERSION (FOR UPDATE
SKIP LOCKED, oldest occurred_at first, ties on id), maps it (extract.py) and upserts its episode in
the same transaction. It never touches Neo4j.

load takes one subgraph ({tenant}_{source}) at a time under a transaction-scoped advisory lock,
writes up to LOAD_BATCH (default 100) of its unloaded episodes in (occurred_at, id) order, and
stamps their loaded_at in that transaction once Neo4j has them. The writers apply each node and
edge by occurred_at, so replays and late, older versions change nothing they shouldn't.

Failures (architecture.md, Failures and replay): an unreachable Postgres or Neo4j is transient: the
job rolls back and is retried with backoff, and nothing is marked done. A record the mappers skip or
reject, or an episode Neo4j refuses, is permanent: logged and counted, and left alone until the
worker restarts. There is no error table; the job queue's dead letter replaces this.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from collections import Counter

import asyncpg
from sqlalchemy import exc as sa_exc
from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import db
from .db import Episode, SourceRecord
from .extract import EXTRACTED, EXTRACTOR_VERSION, SourceRow, extract, graph_records
from .mappers import Reject, Skip
from .records import ordering_time

LOG = logging.getLogger("loader")
POLL_SECONDS = 30.0
CONCURRENCY = 4
LOAD_BATCH = 100
MAX_BACKOFF = 60.0


def is_transient(exc: BaseException) -> bool:
    """A dependency that is down or restarting, which a retry can fix, as opposed to bad data."""
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    if isinstance(exc, (asyncpg.PostgresConnectionError, asyncpg.InterfaceError,
                        asyncpg.exceptions.CannotConnectNowError, asyncpg.exceptions.AdminShutdownError)):
        return True
    if isinstance(exc, sa_exc.DBAPIError) and (exc.connection_invalidated or isinstance(exc, (
            sa_exc.OperationalError, sa_exc.InterfaceError))):
        return True
    try:
        from neo4j import exceptions as neo4j_exc
    except ImportError:  # pragma: no cover - neo4j is a pipeline requirement
        return False
    return isinstance(exc, (neo4j_exc.ServiceUnavailable, neo4j_exc.SessionExpired, neo4j_exc.TransientError))


class Worker:
    """What extract and load share: runners, LISTEN, the poll, and stopping."""

    name = "worker"

    def __init__(self, database_url: str, *, channel: str, concurrency: int = CONCURRENCY,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self.database_url = database_url
        self.channel = channel
        self.concurrency = max(1, concurrency)
        self.poll_seconds = poll_seconds
        self.engine = db.engine(database_url, self.concurrency + 1)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.stopping = asyncio.Event()
        self.listening = asyncio.Event()
        self.kicks = [asyncio.Event() for _ in range(self.concurrency)]
        self.counts: Counter = Counter()

    async def start(self) -> None:
        """Before the runners: a hook for checks and startup logs."""

    async def close(self) -> None:
        """After the runners have finished."""

    async def work_once(self) -> bool:
        """Do one job. False when nothing is waiting."""
        raise NotImplementedError

    def idle_seconds(self) -> float:
        return self.poll_seconds

    def stop(self) -> None:
        """Stop claiming. Jobs in flight finish first."""
        self.stopping.set()
        self.kick_all()

    def kick_all(self) -> None:
        for kick in self.kicks:
            kick.set()

    async def run(self) -> None:
        await self.start()
        listener = asyncio.create_task(self._listen(), name=f"{self.name}-listen")
        runners = [asyncio.create_task(self._runner(i), name=f"{self.name}-{i}") for i in range(self.concurrency)]
        LOG.info("%s started: concurrency %s, LISTEN %s, poll every %ss",
                 self.name, self.concurrency, self.channel, self.poll_seconds)
        try:
            await asyncio.gather(*runners)
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            await self.close()
            await self.engine.dispose()
            LOG.info("%s stopped: %s", self.name, dict(self.counts) or "nothing done")

    async def drain(self) -> None:
        """Work until nothing is waiting, without LISTEN: for --once and tests."""
        await self.start()
        try:
            while not self.stopping.is_set() and await self.work_once():
                pass
        finally:
            await self.close()
            await self.engine.dispose()

    async def _runner(self, index: int) -> None:
        kick = self.kicks[index]
        failures = 0
        while not self.stopping.is_set():
            kick.clear()
            try:
                worked = await self.work_once()
                failures = 0
            except Exception as exc:  # Postgres itself failed; the row stays pending
                failures += 1
                wait = min(self.poll_seconds, MAX_BACKOFF, 2.0 ** failures)
                if is_transient(exc):
                    LOG.warning("%s: %s, retrying in %.0fs", self.name, _error_text(exc), wait)
                else:
                    LOG.exception("%s: job failed, retrying in %.0fs", self.name, wait)
                await self._pause(wait, kick)
                continue
            if not worked:
                await self._pause(self.idle_seconds(), kick)

    async def _pause(self, seconds: float, kick: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(kick.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _notified(self, *_args) -> None:
        self.kick_all()

    async def _listen(self) -> None:
        delay = 1.0
        while not self.stopping.is_set():
            lost = asyncio.Event()
            try:
                conn = await asyncpg.connect(self.database_url)
                try:
                    conn.add_termination_listener(lambda _conn: lost.set())
                    await conn.add_listener(self.channel, self._notified)
                    self.listening.set()
                    LOG.info("%s listening on %s", self.name, self.channel)
                    self.kick_all()  # rows that arrived while nothing was listening
                    delay = 1.0
                    await lost.wait()
                finally:
                    self.listening.clear()
                    await conn.close(timeout=5)
                raise ConnectionError("connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 reconnect; the poll covers the gap
                LOG.warning("LISTEN %s lost (%s); reconnecting in %.0fs", self.channel, _error_text(exc), delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class ExtractWorker(Worker):
    name = "extract"

    def __init__(self, database_url: str, **kwargs) -> None:
        kwargs.setdefault("channel", os.environ.get("SOURCE_RECORDS_CHANNEL", "source_records"))
        super().__init__(database_url, **kwargs)
        # Records skipped or rejected, left alone until a restart (no error table; the queue's dead letter replaces it)
        self.given_up: set[int] = set()

    async def start(self) -> None:
        """Log what is stored but not extracted yet (GitHub, GitLab, other kinds), once."""
        async with self.sessions() as session:
            rows = (await session.execute(
                select(SourceRecord.source, SourceRecord.kind, func.count())
                .where(tuple_(SourceRecord.source, SourceRecord.kind).not_in(sorted(EXTRACTED)))
                .group_by(SourceRecord.source, SourceRecord.kind)
                .order_by(SourceRecord.source, SourceRecord.kind))).all()
        for source, kind, count in rows:
            self.counts[f"not_extracted:{source}:{kind}"] = count
            LOG.info("extract: %s %s source_records %s not extracted (no extractor for that kind yet)",
                     count, source, kind)

    def _claim(self):
        extracted = (select(literal(1)).where(Episode.source_record_id == SourceRecord.id,
                                              Episode.extractor_version >= EXTRACTOR_VERSION).exists())
        stmt = (select(SourceRecord.id, SourceRecord.source, SourceRecord.tenant, SourceRecord.kind,
                       SourceRecord.object_id, SourceRecord.version, SourceRecord.occurred_at, SourceRecord.record)
                .where(tuple_(SourceRecord.source, SourceRecord.kind).in_(sorted(EXTRACTED)), ~extracted))
        if self.given_up:
            stmt = stmt.where(SourceRecord.id.not_in(list(self.given_up)))
        # The source's order, not arrival order (architecture.md, Source ordering value); ties on id
        return (stmt.order_by(SourceRecord.occurred_at, SourceRecord.id).limit(1)
                .with_for_update(skip_locked=True, of=SourceRecord))

    async def work_once(self) -> bool:
        async with self.sessions() as session, session.begin():
            found = (await session.execute(self._claim())).mappings().first()
            if found is None:
                return False
            row = SourceRow(**found)

            async def issue_key(tenant: str, issue_id: str) -> str | None:
                return await session.scalar(
                    select(SourceRecord.record["payload"]["issue"]["key"].astext)
                    .where(SourceRecord.source == "jira", SourceRecord.tenant == tenant,
                           SourceRecord.kind == "issue", SourceRecord.object_id == issue_id)
                    .order_by(SourceRecord.occurred_at.desc(), SourceRecord.id.desc()).limit(1))

            try:
                draft = await extract(row, issue_key)
            except Skip as skip:
                self._give_up(row, "skipped", f"not mapped ({skip.kind})", logging.INFO)
                return True
            except Reject as exc:
                self._give_up(row, "rejected", str(exc), logging.ERROR)
                return True
            except Exception as exc:
                if is_transient(exc):
                    raise
                self._give_up(row, "rejected", f"extract error: {_error_text(exc)}", logging.ERROR)
                return True
            for warning in draft.warnings:
                LOG.warning("source_records %s: %s", row.id, warning)

            values = dict(key=draft.key, tenant=draft.tenant, source=draft.source, kind=draft.kind,
                          occurred_at=draft.occurred_at, text=draft.text, attrs=draft.attrs,
                          source_record_id=draft.source_record_id, extractor_version=EXTRACTOR_VERSION)
            insert = pg_insert(Episode).values(**values)
            result = await session.execute(insert.on_conflict_do_update(
                index_elements=["key"],
                set_={**{name: insert.excluded[name] for name in values if name != "key"},
                      "loaded_at": None, "updated_at": func.now()},
                where=insert.excluded.extractor_version > Episode.extractor_version,
            ))
            if result.rowcount == 0:  # an episode under this key from this extractor version already exists
                self._give_up(row, "rejected", f"episode {draft.key} already exists for another record",
                              logging.ERROR)
                return True
            self.counts["extracted"] += 1
            LOG.info("extracted source_records %s (%s %s %s) into episode %s",
                     row.id, row.source, row.kind, row.object_id, draft.key)
            return True

    def _give_up(self, row: SourceRow, outcome: str, reason: str, level: int) -> None:
        self.given_up.add(row.id)
        self.counts[outcome] += 1
        self.counts[f"{outcome}:{row.source}:{row.kind}"] += 1
        LOG.log(level, "source_records %s (%s %s %s) %s, left alone until restart: %s",
                row.id, row.source, row.kind, row.object_id, outcome, reason)


class LoadWorker(Worker):
    name = "load"

    def __init__(self, database_url: str, writer, *, batch: int = LOAD_BATCH, **kwargs) -> None:
        kwargs.setdefault("channel", os.environ.get("EPISODES_CHANNEL", "episodes"))
        super().__init__(database_url, **kwargs)
        self.writer = writer
        self.batch = batch
        self.schema_ready = False
        self.busy: set[tuple[str, str]] = set()  # subgraphs a runner of this process is loading
        self.cooldown: dict[tuple[str, str], float] = {}  # subgraph -> monotonic time it may retry
        self.failures: Counter = Counter()  # subgraph -> transient failures in a row
        self.given_up: set[int] = set()  # episodes Neo4j refused; left alone until restart

    async def close(self) -> None:
        if hasattr(self.writer, "close"):
            await asyncio.to_thread(self.writer.close)

    def idle_seconds(self) -> float:
        now = time.monotonic()
        waits = [until - now for until in self.cooldown.values() if until > now]
        return max(0.05, min([self.poll_seconds, *waits]))

    async def _waiting_subgraphs(self) -> list[tuple[str, str]]:
        stmt = (select(Episode.tenant, Episode.source)
                .where(Episode.loaded_at.is_(None))
                .group_by(Episode.tenant, Episode.source)
                .order_by(func.min(Episode.occurred_at)))
        if self.given_up:
            stmt = stmt.where(Episode.id.not_in(list(self.given_up)))
        async with self.sessions() as session:
            return [(tenant, source) for tenant, source in (await session.execute(stmt)).all()]

    async def work_once(self) -> bool:
        now = time.monotonic()
        for subgraph in await self._waiting_subgraphs():
            if subgraph in self.busy or self.cooldown.get(subgraph, 0) > now:
                continue
            self.busy.add(subgraph)
            try:
                if await self.load_subgraph(*subgraph):
                    self.failures.pop(subgraph, None)
                    self.cooldown.pop(subgraph, None)
                    return True
            except Exception as exc:
                if not is_transient(exc):
                    raise
                self.failures[subgraph] += 1
                wait = min(self.poll_seconds, MAX_BACKOFF, 2.0 ** self.failures[subgraph])
                self.cooldown[subgraph] = time.monotonic() + wait
                self.counts["transient_failures"] += 1
                LOG.warning("load %s_%s: %s; its episodes stay unloaded, retrying in %.0fs",
                            *subgraph, _error_text(exc), wait)
            finally:
                self.busy.discard(subgraph)
        return False

    async def load_subgraph(self, tenant: str, source: str) -> bool:
        """Write the next batch of one subgraph's episodes and stamp them. False if another worker
        holds the subgraph or nothing is waiting."""
        async with self.sessions() as session, session.begin():
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(func.hashtext(f"{tenant}_{source}"))))
            if not locked:
                return False
            stmt = (select(Episode.id, Episode.key, Episode.occurred_at, Episode.attrs)
                    .where(Episode.tenant == tenant, Episode.source == source, Episode.loaded_at.is_(None))
                    .order_by(Episode.occurred_at, Episode.id).limit(self.batch))
            if self.given_up:
                stmt = stmt.where(Episode.id.not_in(list(self.given_up)))
            episodes = (await session.execute(stmt)).all()
            if not episodes:
                return False
            loaded = await asyncio.to_thread(self._write, episodes)
            if loaded:
                await session.execute(update(Episode).where(Episode.id.in_(loaded))
                                      .values(loaded_at=func.clock_timestamp()))
        self.counts["loaded"] += len(loaded)
        LOG.info("loaded %s of %s episodes into %s_%s", len(loaded), len(episodes), tenant, source)
        return True

    def _write(self, episodes) -> list[int]:
        """Write episodes to the graph, in order. Returns the ids written. A transient error raises,
        so nothing is stamped; if the batch fails otherwise, it is retried one episode at a time
        and the ones that still fail are given up."""
        if not self.schema_ready:
            self.writer.ensure_schema()
            self.schema_ready = True
        try:
            self._write_batch(episodes)
            return [episode.id for episode in episodes]
        except Exception as exc:
            if is_transient(exc):
                raise
            if len(episodes) == 1:
                self._give_up(episodes[0], exc)
                return []
        written = []
        for episode in episodes:
            try:
                self._write_batch([episode])
                written.append(episode.id)
            except Exception as exc:
                if is_transient(exc):
                    raise
                self._give_up(episode, exc)
        return written

    def _give_up(self, episode, exc: BaseException) -> None:
        self.given_up.add(episode.id)
        self.counts["failed"] += 1
        LOG.error("episode %s (%s) not loaded, left alone until restart: %s", episode.id, episode.key,
                  _error_text(exc))

    def _write_batch(self, episodes) -> None:
        """Every node of the batch, then every edge, so an edge finds nodes from later episodes."""
        nodes, edges = [], []
        for episode in episodes:
            batch_nodes, batch_edges = graph_records(episode.attrs, ordering_time(episode.occurred_at))
            nodes += batch_nodes
            edges += batch_edges
        self.writer.write_nodes(_dedupe(nodes, lambda n: (n.label, n.uuid)))
        self.writer.write_edges(_dedupe(edges, lambda e: (e.type, e.src_uuid, e.dst_uuid)), placeholders=True)
        self.counts["node_rows"] += len(nodes)
        self.counts["edge_rows"] += len(edges)


def _dedupe(records: list, key) -> list:
    """Collapse consecutive rows for one element with the same props to the last (newest) one;
    applying them all would end in the same state. Keeps the order of everything else."""
    out: list = []
    last: dict = {}
    for record in records:
        k = key(record)
        previous = last.get(k)
        if previous is not None and out[previous].props == record.props:
            out[previous] = None
        last[k] = len(out)
        out.append(record)
    return [record for record in out if record is not None]


def neo4j_writer():
    from .neo4j_writer import Neo4jWriter

    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("NEO4J_PASSWORD is not set")
    return Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                       password, database=os.environ.get("NEO4J_DATABASE") or None)


async def serve(worker: Worker, once: bool) -> None:
    """Run the worker until SIGINT or SIGTERM (or, with once, until nothing is waiting)."""
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        if worker.stopping.is_set():
            LOG.warning("second interrupt: exiting now; unfinished work stays pending in Postgres")
            os._exit(130)
        LOG.info("stopping: finishing the jobs in flight (interrupt again to exit now)")
        worker.stop()

    job = worker.drain() if once else worker.run()
    if sys.platform == "win32":  # Windows event loops have no add_signal_handler
        previous = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(on_signal))
        try:
            await job
        finally:
            signal.signal(signal.SIGINT, previous)
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)
    try:
        await job
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="loader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("extract", "load"))
    parser.add_argument("--once", action="store_true", help="work until nothing is waiting, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    for name in ("neo4j", "neo4j.notifications"):
        logging.getLogger(name).setLevel(logging.ERROR)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Set DATABASE_URL (on the cluster, Secret postgres-app's uri)", file=sys.stderr)
        return 2
    try:
        concurrency = int(os.environ.get("CONCURRENCY", CONCURRENCY))
        batch = int(os.environ.get("LOAD_BATCH", LOAD_BATCH))
    except ValueError:
        print("CONCURRENCY and LOAD_BATCH must be whole numbers", file=sys.stderr)
        return 2

    async def start() -> None:
        if args.stage == "extract":
            worker: Worker = ExtractWorker(database_url, concurrency=concurrency)
        else:
            worker = LoadWorker(database_url, neo4j_writer(), concurrency=concurrency, batch=batch)
        await serve(worker, args.once)

    asyncio.run(start())
    return 0
