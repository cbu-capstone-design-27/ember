"""add_episode, one row at a time in the source's order, with a ledger so a stopped run picks up where it left off.

The Graphiti object and the graph reader are passed in, so the fork and the tests can use their own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from ontology import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, subgraph_id

from .data import Row, render
from .metrics import Counters, delta

log = logging.getLogger("graphiti_test")

DONE = ("ok", "already_in_graph")
STOP_AFTER_CONSECUTIVE_FAILURES = 5


class Ledger:
    """One JSON line per attempt, flushed to disk before the next one. The last line for an episode wins."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:  # a run killed mid-write leaves a torn last line; that episode just runs again
                    continue
                self.entries[entry["name"]] = entry

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.entries[entry["name"]] = entry

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self.entries = {}


def classify(exc: BaseException) -> str:
    """Throttled, transient or permanent, as in docs/ingestion/architecture.md (Failures and replay), with the
    permanent ones split: structured-output failures are ADR 0002's open risk."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    module = type(exc).__module__.split(".")[0]
    if "RateLimitError" in names:
        return "throttled"
    if names & {"ValidationError", "JSONDecodeError", "EmptyResponseError"}:
        return "structured_output"
    if names & {"APITimeoutError", "APIConnectionError", "InternalServerError", "TimeoutError", "ConnectionError"}:
        return "transient"
    if module == "neo4j":
        return "graph_database"
    return "other"


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def describe_error(exc: BaseException, secrets: tuple[str, ...] = ()) -> str:
    """Pydantic messages quote the model's input, so those give field paths and error types only."""
    errors = getattr(exc, "errors", None)
    if type(exc).__name__ == "ValidationError" and callable(errors):
        fields = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in errors()[:5])
        return redact(f"{getattr(exc, 'title', '')}: {fields}", secrets)
    return redact(str(exc), secrets)[:500]


async def ingest(graphiti, rows: list[Row], tenant: str, ledger: Ledger, counters: Counters,
                 episode_type: Callable[[str], object], existing: set[str], secrets: tuple[str, ...] = ()) -> dict:
    """Adds every row the ledger and the graph do not already hold. Failed rows are retried on the next run."""
    newest: dict[str, str] = {}
    for row in rows:
        entry = ledger.entries.get(row.name)
        if entry and entry["status"] in DONE:
            group = subgraph_id(tenant, row.source)
            newest[group] = max(newest.get(group, ""), row.occurred_at.isoformat())

    summary = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "out_of_order": 0, "stopped_early": False}
    consecutive = 0
    for position, row in enumerate(rows, 1):
        entry = ledger.entries.get(row.name)
        if entry and entry["status"] in DONE:
            summary["skipped"] += 1
            continue
        base = {"name": row.name, "occurred_at": row.occurred_at.isoformat()}
        if row.name in existing:  # written, then the run died before the ledger line
            ledger.append({**base, "status": "already_in_graph"})
            summary["skipped"] += 1
            continue

        group = subgraph_id(tenant, row.source)
        if row.occurred_at.isoformat() < newest.get(group, ""):
            summary["out_of_order"] += 1
            log.warning("%s is older than what this subgraph already holds: a grown --limit on an existing graph "
                        "applies changes out of the source's order. Use a fresh graph for a reference.", row.name)
        newest[group] = max(newest.get(group, ""), row.occurred_at.isoformat())

        before, started = counters.snapshot(), time.monotonic()
        summary["attempted"] += 1
        arguments = render(row)
        arguments["source"] = episode_type(arguments["source"])
        try:
            result = await graphiti.add_episode(
                **arguments, reference_time=row.occurred_at, group_id=group,
                entity_types=ENTITY_TYPES, edge_types=EDGE_TYPES, edge_type_map=EDGE_TYPE_MAP)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  every failure is data here; Ctrl+C still stops the run
            summary["failed"] += 1
            consecutive += 1
            ledger.append({**base, "status": "failed", "failure": classify(exc), "error_type": type(exc).__name__,
                           "error": describe_error(exc, secrets), "wall_s": round(time.monotonic() - started, 3),
                           "counters": delta(counters.snapshot(), before)})
            log.warning("[%d/%d] %s failed: %s", position, len(rows), row.name, type(exc).__name__)
            if consecutive >= STOP_AFTER_CONSECUTIVE_FAILURES:
                log.error("stopping after %d failures in a row; rerun to resume", consecutive)
                summary["stopped_early"] = True
                break
            continue
        consecutive = 0
        summary["ok"] += 1
        ledger.append({**base, "status": "ok", "wall_s": round(time.monotonic() - started, 3),
                       "counters": delta(counters.snapshot(), before),
                       "nodes": len(result.nodes), "edges": len(result.edges)})
        log.info("[%d/%d] %s ok", position, len(rows), row.name)
    return summary
