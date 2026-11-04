"""Runs every question through Graphiti's hybrid search under each reranker, and the "what was true then" check.

Search and rerank go through graphiti.search_, stock. The fork runs this file unchanged.
"""

from __future__ import annotations

import asyncio
import time

from graphiti_core.search.search_config import SearchConfig
from graphiti_core.search.search_config_recipes import (
    COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
    COMBINED_HYBRID_SEARCH_MMR,
    COMBINED_HYBRID_SEARCH_RRF,
    EDGE_HYBRID_SEARCH_EPISODE_MENTIONS,
    EDGE_HYBRID_SEARCH_NODE_DISTANCE,
    EDGE_HYBRID_SEARCH_RRF,
    NODE_HYBRID_SEARCH_EPISODE_MENTIONS,
    NODE_HYBRID_SEARCH_NODE_DISTANCE,
)
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

from .data import TEMPORAL
from .ingest import classify, describe_error
from .metrics import Counters, delta
from .report import top_k

LIMIT = 10


def _without_communities(recipe: SearchConfig) -> SearchConfig:
    """Ember does not build communities (research Part 2), so that scope would only ever be empty."""
    return recipe.model_copy(update={"community_config": None, "limit": LIMIT})


def _combined(edges: SearchConfig, nodes: SearchConfig) -> SearchConfig:
    """node_distance and episode_mentions have per-scope recipes only; episodes have no such reranker, so they keep
    the combined recipe's bm25 + RRF."""
    return SearchConfig(edge_config=edges.edge_config, node_config=nodes.node_config,
                        episode_config=COMBINED_HYBRID_SEARCH_RRF.episode_config, limit=LIMIT)


RERANKERS: dict[str, SearchConfig] = {
    "rrf": _without_communities(COMBINED_HYBRID_SEARCH_RRF),
    "mmr": _without_communities(COMBINED_HYBRID_SEARCH_MMR),
    "cross_encoder": _without_communities(COMBINED_HYBRID_SEARCH_CROSS_ENCODER),
    "node_distance": _combined(EDGE_HYBRID_SEARCH_NODE_DISTANCE, NODE_HYBRID_SEARCH_NODE_DISTANCE),
    "episode_mentions": _combined(EDGE_HYBRID_SEARCH_EPISODE_MENTIONS, NODE_HYBRID_SEARCH_EPISODE_MENTIONS),
}
# node_distance needs a centre: the top node of this question's rrf result (how research Part 2 would use it).
NEEDS_CENTER = {"node_distance"}


async def _view(results, reader) -> dict:
    """Results by natural identifiers only, so runs on another backend compare."""
    edge_episodes = sorted({u for e in results.edges for u in e.episodes})
    edge_records = await reader.records_of_episodes(edge_episodes) if edge_episodes else {}
    ends = sorted({u for e in results.edges for u in (e.source_node_uuid, e.target_node_uuid)})
    names = await reader.node_names(ends) if ends else {}
    mentions = await reader.records_mentioning([n.uuid for n in results.nodes]) if results.nodes else {}
    episode_records = await reader.records_of_episodes([e.uuid for e in results.episodes]) if results.episodes else {}
    return {
        "edges": [{"type": e.name, "source": names.get(e.source_node_uuid), "target": names.get(e.target_node_uuid),
                   "fact": e.fact, "records": sorted({edge_records[u] for u in e.episodes if u in edge_records})}
                  for e in results.edges],
        "nodes": [{"name": n.name, "labels": sorted(n.labels), "records": mentions.get(n.uuid, [])}
                  for n in results.nodes],
        "episodes": [episode_records[e.uuid] for e in results.episodes if e.uuid in episode_records],
    }


def ranked_records(view: dict) -> dict[str, list[str]]:
    """Each scope's records, first appearance first, deduplicated."""
    out = {"edges": [r for e in view["edges"] for r in e["records"]],
           "nodes": [r for n in view["nodes"] for r in n["records"]],
           "episodes": list(view["episodes"])}
    return {scope: list(dict.fromkeys(records)) for scope, records in out.items()}


async def run_search(graphiti, reader, counters: Counters, groups: list[str], questions: list[dict],
                     query_vector, secrets: tuple[str, ...] = ()) -> tuple[list[dict], list[dict]]:
    """Returns (comparable results, measurements), one row of each per question and reranker, in the same order."""
    results, measures = [], []
    for question in questions:
        vector = await query_vector(question["query"])
        center = None
        for reranker, config in RERANKERS.items():
            row = {"id": question["id"], "query": question["query"], "expected": question["expected"],
                   "reranker": reranker}
            measure = {"id": question["id"], "reranker": reranker}
            if reranker in NEEDS_CENTER and center is None:
                results.append({**row, "skipped": "no node to centre on"})
                measures.append(measure)
                continue
            before, started = counters.snapshot(), time.monotonic()
            try:
                found = await graphiti.search_(
                    question["query"], config=config, group_ids=groups, query_vector=vector,
                    center_node_uuid=center if reranker in NEEDS_CENTER else None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  a reranker that fails is a finding, not the end of the run
                results.append({**row, "error": {"failure": classify(exc), "type": type(exc).__name__,
                                                 "message": describe_error(exc, secrets)}})
                measures.append({**measure, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                                 "counters": delta(counters.snapshot(), before)})
                continue
            latency = round((time.monotonic() - started) * 1000, 1)
            if reranker == "rrf" and found.nodes:
                center = found.nodes[0].uuid
            view = await _view(found, reader)
            ranked = ranked_records(view)
            results.append({**row, **top_k(ranked, question["expected"]), "ranked": view})
            measures.append({**measure, "latency_ms": latency, "counters": delta(counters.snapshot(), before),
                             "edge_scores": [round(s, 6) for s in found.edge_reranker_scores],
                             "node_scores": [round(s, 6) for s in found.node_reranker_scores]})
    return results, measures


def _as_of(moment) -> SearchFilters:
    """Facts true at `moment`: started by then, and not yet ended."""
    return SearchFilters(
        valid_at=[[DateFilter(date=moment, comparison_operator=ComparisonOperator.less_than_equal)]],
        invalid_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)],
                    [DateFilter(date=moment, comparison_operator=ComparisonOperator.greater_than)]])


async def run_temporal(graphiti, reader, groups: list[str], query_vector) -> dict:
    """The same question as of two moments, and what Graphiti ended. On real models the facts' wording varies, so the
    verdict looks only for the old and the new term."""
    config = EDGE_HYBRID_SEARCH_RRF.model_copy(update={"limit": LIMIT})
    vector = await query_vector(TEMPORAL["query"])
    moments = {}
    for label in ("before", "after"):
        found = await graphiti.search_(TEMPORAL["query"], config=config, group_ids=groups, query_vector=vector,
                                       search_filter=_as_of(TEMPORAL[label]))
        facts = [e.fact for e in found.edges]
        moments[label] = {"at": TEMPORAL[label].isoformat(), "facts": facts,
                          "old_term": any(TEMPORAL["old_term"] in f.lower() for f in facts),
                          "new_term": any(TEMPORAL["new_term"] in f.lower() for f in facts)}
    invalidated = await reader.invalidated(groups)
    # Later facts can still name the old stack ("GPUI did not pass"), so the verdict follows the ended facts themselves.
    ended = {e["fact"] for e in invalidated if TEMPORAL["old_term"] in e["fact"].lower()}
    return {
        "id": TEMPORAL["id"], "query": TEMPORAL["query"], "moments": moments,
        "invalidated": invalidated,
        "old_fact_ended": bool(ended),
        "old_fact_seen_before": bool(ended & set(moments["before"]["facts"])),
        "old_fact_gone_after": bool(ended) and not ended & set(moments["after"]["facts"]),
    }
