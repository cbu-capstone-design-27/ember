"""The reference a Graphiti run leaves behind: reference.json, summary.md, and how two references compare.

reference.json has two halves:

- `comparable`: what the graph holds and what search returns, by natural identifiers (names, facts, record ids), no
  uuids, times or scores. `python -m graphiti_test diff a.json b.json` compares only this. The Postgres fork must
  match it, apart from ranking differences someone has accepted.
- `measured`: what this run cost and how it ran. Differs on every run.

Stdlib only. FORMAT changes when a field's meaning changes, and diff refuses to compare across formats.
"""

from __future__ import annotations

import json
from pathlib import Path

FORMAT = "ember-graphiti-reference/v1"
TOP = 3
FAKE_WARNING = ("FAKE MODELS: this run used the local fake, whose answers are regexes over the prompts. It says "
                "nothing about extraction quality, retrieval quality or cost. It only proves the plumbing and the "
                "graph database.")


def top_k(ranked: dict[str, list[str]], expected: list[str], k: int = TOP) -> dict:
    """Whether an expected record is in each scope's top k, and in any scope's."""
    in_scope = {scope: any(r in expected for r in records[:k]) for scope, records in ranked.items()}
    return {"in_top3": any(in_scope.values()), "in_top3_by_scope": in_scope}


def build_reference(*, fake: bool, engine: dict, models: dict, run: dict, ingest: dict, ledger_entries: dict,
                    graph: dict, isolation: dict, search: list[dict], search_measures: list[dict],
                    temporal: dict | None, resend: dict | None, compatibility: dict) -> dict:
    return {
        "format": FORMAT,
        "fake_models": fake,
        **({"warning": FAKE_WARNING} if fake else {}),
        "comparable": {
            "tenant": run["tenant"],
            "selected_records": sorted(run["selected"]),
            "failed_records": sorted(n for n, e in ledger_entries.items() if e["status"] == "failed"),
            "graph": graph,
            "isolation": isolation,
            "search": search,
            "temporal": temporal,
            "resend": resend,
        },
        "measured": {
            "engine": engine,
            "models": models,
            "run": {k: v for k, v in run.items() if k != "selected"},
            "compatibility": compatibility,
            "ingest": ingest,
            "episodes": aggregate_episodes(ledger_entries),
            "search": search_measures,
        },
    }


def _spread(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "min": ordered[0], "median": ordered[len(ordered) // 2], "max": ordered[-1],
            "total": round(sum(ordered), 3)}


def aggregate_episodes(entries: dict) -> dict:
    """Per-episode cost, from the ledger. By prompt: how often it ran per episode, and at what size."""
    done = [e for e in entries.values() if e["status"] in ("ok", "failed") and "counters" in e]
    prompts: dict[str, dict] = {}
    failures: dict[str, int] = {}
    for entry in done:
        for prompt, row in entry["counters"]["prompts"].items():
            total = prompts.setdefault(prompt, {"calls": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0})
            for key in total:
                total[key] += row[key]
        if entry["status"] == "failed":
            failures[entry["failure"]] = failures.get(entry["failure"], 0) + 1

    def role(entry, name, key):
        return entry["counters"]["roles"].get(name, {}).get(key, 0)

    return {
        "wall_s": _spread([e["wall_s"] for e in done]),
        "llm_calls": _spread([sum(p["calls"] for p in e["counters"]["prompts"].values()) for e in done]),
        "llm_requests": _spread([role(e, "llm", "requests") for e in done]),
        "llm_input_tokens": _spread([role(e, "llm", "input_tokens") for e in done]),
        "llm_output_tokens": _spread([role(e, "llm", "output_tokens") for e in done]),
        "embedding_requests": _spread([role(e, "embedding", "requests") for e in done]),
        "embedding_inputs": _spread([role(e, "embedding", "inputs") for e in done]),
        "structured_failures": sum(e["counters"]["structured_failures"] for e in done),
        "episode_failures_by_class": failures,
        "by_prompt": {k: prompts[k] for k in sorted(prompts)},
        "failed": [{k: e.get(k) for k in ("name", "failure", "error_type", "error")}
                   for e in entries.values() if e["status"] == "failed"],
    }


def write_reference(reference: dict, out: Path, structured_failures: list | None = None) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    if structured_failures is not None:
        reference["measured"]["structured_failures"] = structured_failures
    json_path, md_path = out / "reference.json", out / "summary.md"
    json_path.write_text(json.dumps(reference, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_summary(reference), encoding="utf-8")
    return json_path, md_path


def render_summary(reference: dict) -> str:
    comparable, measured = reference["comparable"], reference["measured"]
    episodes, ingest = measured["episodes"], measured["ingest"]
    engine, compat = measured["engine"], measured["compatibility"]
    lines = ["# Graphiti test run", ""]
    if reference["fake_models"]:
        lines += [f"**{reference['warning']}**", ""]
    lines += [
        f"- Tenant `{comparable['tenant']}`, {len(comparable['selected_records'])} records selected, "
        f"profile `{measured['models']['profile']}`, graphiti-core {engine['graphiti_core']}, "
        f"{engine['server'].get('name')} {engine['server'].get('version')} {engine['server'].get('edition')}.",
        f"- Ingest: {ingest['ok']} ok, {ingest['failed']} failed, {ingest['skipped']} skipped (already done), "
        f"{ingest['out_of_order']} out of order"
        + (", **stopped early**" if ingest["stopped_early"] else "") + ".",
        f"- Indexes: {compat['indexes_online']} online, {compat['indexes_not_online']} not online."
        + (f" Build error: {compat['index_build_error']}" if compat["index_build_error"] else ""),
        f"- Server notifications: {len(compat['notifications'])}. Subgraph-crossing edges: "
        f"{comparable['isolation']['crossing_edges']}.", "",
        "## Cost per episode", "",
        "| Measure | median | min | max | total |", "| --- | ---: | ---: | ---: | ---: |"]
    for key in ("wall_s", "llm_calls", "llm_requests", "llm_input_tokens", "llm_output_tokens",
                "embedding_requests", "embedding_inputs"):
        s = episodes[key]
        if s["n"]:
            lines.append(f"| {key} | {s['median']} | {s['min']} | {s['max']} | {s['total']} |")
    lines += ["", f"Structured-output failures: {episodes['structured_failures']}. Failed episodes by class: "
              f"{episodes['episode_failures_by_class'] or 'none'}.", "",
              "| Prompt | calls | HTTP requests | input tokens | output tokens |", "| --- | ---: | ---: | ---: | ---: |"]
    for prompt, row in episodes["by_prompt"].items():
        lines.append(f"| {prompt} | {row['calls']} | {row['requests']} | {row['input_tokens']} | {row['output_tokens']} |")

    lines += ["", "## Graph", "", "| Subgraph | Episodes | Entities | Edges | Off the ontology map | No SourceRef |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for group, row in comparable["graph"].items():
        lines.append(f"| {group} | {row['episodes']} | {row['entities']} | {row['edges']} | "
                     f"{sum(row['edges_off_the_ontology_map'].values())} | "
                     f"{sum(row['entities_missing_source_ref'].values())} |")

    latency = {(m["id"], m["reranker"]): m.get("latency_ms") for m in measured["search"]}
    reranks = list(dict.fromkeys(r["reranker"] for r in comparable["search"]))
    lines += ["", "## Search: expected record in the top 3", "",
              "| Question | " + " | ".join(reranks) + " |", "| --- |" + " --- |" * len(reranks)]
    by_question: dict[str, dict] = {}
    for row in comparable["search"]:
        by_question.setdefault(row["id"], {})[row["reranker"]] = row
    for qid, rows in by_question.items():
        cells = []
        for name in reranks:
            row = rows.get(name, {})
            if "error" in row:
                cells.append(f"error ({row['error']['type']})")
            elif "skipped" in row:
                cells.append("skipped")
            else:
                cells.append(f"{'yes' if row.get('in_top3') else 'no'} ({latency.get((qid, name))} ms)")
        lines.append(f"| {qid} | " + " | ".join(cells) + " |")

    temporal = comparable["temporal"]
    lines += ["", "## What was true then", ""]
    if temporal:
        lines += [f"Question: {temporal['query']}", "",
                  f"- Old fact visible before the change: {temporal['old_fact_seen_before']}.",
                  f"- Graphiti ended it: {temporal['old_fact_ended']}.",
                  f"- Gone from what is true after the change: {temporal['old_fact_gone_after']}.",
                  f"- Facts Graphiti ended in all: {len(temporal['invalidated'])}."]
        lines += [f"  - {e['fact'][:120]} (valid {e['valid_at']}, ended {e['invalid_at']})"
                  for e in temporal["invalidated"][:10]]
    resend = comparable["resend"]
    if resend:
        lines += ["", "## Re-sent episode (ADR 0002 criterion 2)", "",
                  f"Entities {resend['entities_added']:+d}, edges {resend['edges_added']:+d}, "
                  f"episodes {resend['episodes_added']:+d}."]
    return "\n".join(lines) + "\n"


def differences(a, b, path: str = "") -> list[str]:
    if type(a) is not type(b):
        return [f"{path}: {json.dumps(a, default=str)[:160]} != {json.dumps(b, default=str)[:160]}"]
    if isinstance(a, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{path}/{key}: only in the second")
            elif key not in b:
                out.append(f"{path}/{key}: only in the first")
            else:
                out += differences(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list):
        out = [f"{path}: {len(a)} items != {len(b)} items"] if len(a) != len(b) else []
        for i, (left, right) in enumerate(zip(a, b)):
            out += differences(left, right, f"{path}[{i}]")
        return out
    return [] if a == b else [f"{path}: {json.dumps(a, default=str)[:160]} != {json.dumps(b, default=str)[:160]}"]


def compare(first: dict, second: dict) -> list[str]:
    """The differences in the comparable half. Measurements never count. Refuses mixed formats."""
    if first.get("format") != second.get("format"):
        raise ValueError(f"different formats: {first.get('format')} and {second.get('format')}")
    return differences(first["comparable"], second["comparable"])
