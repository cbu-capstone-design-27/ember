"""One run: ingest, read back, search, the temporal and resend checks, and the reference files."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from models.client import DEFAULT_TASK
from models.config import Profile, load_profile
from ontology import subgraph_id, tenant_subgraphs

from .data import load_rows, questions, select_rows
from .fake_models import KEY as FAKE_KEY
from .fake_models import FakeModels
from .ingest import Ledger, describe_error, ingest
from .metrics import Counters, delta
from .report import build_reference, write_reference

log = logging.getLogger("graphiti_test")


class SetupError(Exception):
    """The run cannot start (exit 2)."""


def _graphiti_environment(profile: Profile) -> None:
    """Must run before graphiti_core is imported: it reads both at import time or on first use.

    Telemetry defaults to on and posts to PostHog from every Graphiti(). Nothing here should leave the machine
    except calls to the profile's own endpoints.
    """
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    os.environ["EMBEDDING_DIM"] = str(profile.embedding_dimensions)


def neo4j_settings(args, env=os.environ) -> tuple[str, str, str, str]:
    uri = args.neo4j_uri or env.get("NEO4J_URI") or "bolt://localhost:7687"
    host = urlsplit(uri).hostname or ""
    if host.startswith("hv-") or host.endswith(".ts.net"):
        raise SetupError(f"refusing {host}: this harness adds Graphiti's indexes and data to the graph it is given, "
                         "so it takes a throwaway Neo4j, never the cluster's")
    password = env.get("NEO4J_PASSWORD")
    if not password:
        raise SetupError("NEO4J_PASSWORD is not set")
    return uri, env.get("NEO4J_USER", "neo4j"), password, env.get("NEO4J_DATABASE", "neo4j")


class _NotificationLog(logging.Handler):
    """The Neo4j driver logs what the server says about a query: deprecations, for instance. Those are compatibility
    evidence."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.seen: dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage().split("\n")[0][:300]
        self.seen[text] = self.seen.get(text, 0) + 1


async def _resend_check(graphiti, reader, rows, tenant, groups, episode_type, marker: Path) -> dict | None:
    """ADR 0002 criterion 2: the first row again, as a source's retry would. Graphiti has no idempotency key of its
    own, so this is what a duplicate does if nothing upstream stops it. Runs once per graph."""
    from ontology import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

    from .data import render
    from .graph_reader import RESEND_SUFFIX

    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    if not rows:
        return None

    async def totals():
        reports = [await reader.group_report(g) for g in groups]
        names = await reader.episode_names(groups)
        return {"entities": sum(r["entities"] for r in reports), "edges": sum(r["edges"] for r in reports),
                "episodes": len(names)}

    row = rows[0]
    before = await totals()
    arguments = render(row)
    arguments["source"] = episode_type(arguments["source"])
    arguments["name"] += RESEND_SUFFIX
    await graphiti.add_episode(**arguments, reference_time=row.occurred_at, group_id=subgraph_id(tenant, row.source),
                               entity_types=ENTITY_TYPES, edge_types=EDGE_TYPES, edge_type_map=EDGE_TYPE_MAP)
    after = await totals()
    result = {"record": row.name, "entities_added": after["entities"] - before["entities"],
              "edges_added": after["edges"] - before["edges"], "episodes_added": after["episodes"] - before["episodes"]}
    marker.write_text(json.dumps(result), encoding="utf-8")
    return result


async def run(args) -> int:
    out = Path(args.out)
    profile = load_profile(args.profile)
    fake = FakeModels(profile.embedding_dimensions) if args.fake else None
    if fake:
        fake.__enter__()
        profile = dataclasses.replace(profile, llm_base_url=fake.base_url, embedding_base_url=fake.base_url)
        llm_key = embedding_key = FAKE_KEY
    else:
        llm_key, embedding_key = profile.llm_key(), profile.embedding_key()
    secrets = (llm_key, embedding_key, os.environ.get("NEO4J_PASSWORD", ""))
    try:
        return await _run(args, out, profile, llm_key, embedding_key, secrets, bool(fake))
    finally:
        if fake:
            fake.__exit__()


async def _run(args, out, profile, llm_key, embedding_key, secrets, is_fake) -> int:
    _graphiti_environment(profile)
    uri, user, password, database = neo4j_settings(args)

    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.nodes import EpisodeType

    from .clients import build_clients
    from .graph_reader import GraphReader
    from .search_suite import run_search, run_temporal

    notifications = _NotificationLog()
    neo4j_log = logging.getLogger("neo4j")  # neo4j.notifications propagates here
    neo4j_log.addHandler(notifications)
    neo4j_log.propagate = bool(args.verbose)  # counted in the reference; one line per query would drown the log

    all_rows = list(load_rows())
    rows = select_rows(all_rows, args.limit)
    groups = tenant_subgraphs(args.tenant)
    counters = Counters()

    driver = Neo4jDriver(uri, user, password, database)
    graphiti = Graphiti(graph_driver=driver, max_coroutines=args.concurrency,
                        **build_clients(profile, llm_key, embedding_key, counters, args.structured_mode))
    reader = GraphReader(driver)
    ledger = Ledger(out / "episodes.jsonl")
    resend_marker = out / "resend.json"
    try:
        if args.reset:
            await reader.reset(groups)
            ledger.clear()
            resend_marker.unlink(missing_ok=True)

        index_error = None
        try:
            await graphiti.build_indices_and_constraints()
        except Exception as exc:  # noqa: BLE001  whether this works is the question on a new Neo4j
            index_error = f"{type(exc).__name__}: {describe_error(exc, secrets)}"
            log.error("index build failed: %s", index_error)
        indexes = await reader.indexes()
        server = await reader.server()

        vector_of = await _query_embedder(graphiti, profile)
        await vector_of("dimension check")  # a wrong dimension should stop the run now, not at the first search

        existing = await reader.episode_names(groups)
        summary = await ingest(graphiti, rows, args.tenant, ledger, counters, lambda name: EpisodeType[name],
                               existing, secrets)

        graph = {g: await reader.group_report(g) for g in groups}
        graph = {g: r for g, r in graph.items() if r["episodes"] or r["entities"]}
        before = counters.snapshot()
        search, measures = await run_search(graphiti, reader, counters, groups, questions(all_rows), vector_of, secrets)
        search_cost = delta(counters.snapshot(), before)
        temporal = await run_temporal(graphiti, reader, groups, vector_of)
        resend = None
        if not args.no_resend_check and not summary["stopped_early"]:
            resend = await _resend_check(graphiti, reader, rows, args.tenant, groups,
                                         lambda name: EpisodeType[name], resend_marker)

        online = [i for i in indexes if i["state"] == "ONLINE"]
        reference = build_reference(
            fake=is_fake,
            engine={"graphiti_core": version("graphiti-core"), "neo4j_driver": version("neo4j"),
                    "openai": version("openai"), "httpx": version("httpx"), "backend": "neo4j", "server": server},
            models={"profile": profile.name, "llm_model": profile.llm_model, "embedding_model": profile.embedding_model,
                    "embedding_dimensions": profile.embedding_dimensions, "send_dimensions": profile.send_dimensions,
                    "query_template": profile.query_template, "query_task": DEFAULT_TASK,
                    "structured_output_mode": args.structured_mode, "llm_client": type(graphiti.llm_client).__name__,
                    "temperature": 0},
            run={"tenant": args.tenant, "limit": args.limit, "selected": [r.name for r in rows],
                 "concurrency": args.concurrency, "total_available": len(all_rows)},
            ingest={**summary, "search_cost": search_cost},
            ledger_entries={r.name: ledger.entries[r.name] for r in rows if r.name in ledger.entries},
            graph=graph,
            isolation={"crossing_edges": await reader.crossing_edges(groups)},
            search=search, search_measures=measures, temporal=temporal, resend=resend,
            compatibility={"indexes_online": len(online), "indexes_not_online": len(indexes) - len(online),
                           "index_build_error": index_error, "indexes": indexes,
                           "notifications": notifications.seen},
        )
        json_path, md_path = write_reference(reference, out, counters.structured_failures)
        log.info("wrote %s and %s", json_path, md_path)
        if is_fake:
            print("FAKE MODELS: this output is not meaningful for quality or cost.", file=sys.stderr)
        failed = summary["failed"] or summary["stopped_early"] or index_error
        return 1 if failed else 0
    finally:
        await graphiti.close()
        neo4j_log.removeHandler(notifications)


async def _query_embedder(graphiti, profile: Profile):
    """The vector a search uses: the profile's query template, then Graphiti's own embedder.

    Stock search_ would embed the bare question, which the Qwen3 model is not meant to be queried with (ADR 0001). It
    takes query_vector, so no fork is needed for this. One vector per question, so reranker runs share it.
    """
    cache: dict[str, list[float]] = {}

    async def vector_of(query: str) -> list[float]:
        if query not in cache:
            text = profile.query_template.format(task=DEFAULT_TASK, query=query)
            vector = await graphiti.embedder.create(input_data=[text])
            if len(vector) != profile.embedding_dimensions:
                raise SetupError(f"embedding has {len(vector)} dimensions, profile {profile.name} expects "
                                 f"{profile.embedding_dimensions}")
            cache[query] = vector
        return cache[query]

    return vector_of
