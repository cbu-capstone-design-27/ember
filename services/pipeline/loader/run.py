"""Read, map, validate, write and check. One call is one pipeline run."""

from __future__ import annotations

import json
from pathlib import Path

from ontology import tenant_subgraphs

from .check import check_graph
from .conform import validate_mapped
from .mappers import MapContext, Reject, Skip, issue_index, map_record
from .reader import input_files, read_envelopes
from .report import Report


class InputError(Exception):
    """The run cannot start because of the input folder or the flags; the CLI exits 2."""


def _merge_props(existing: dict, incoming: dict) -> None:
    """Fold a repeated record into the first one without losing data.

    A None never overwrites a value, and the longer non-empty embed_text wins, so a sparse
    webhook cannot erase what a fuller record already set.
    """
    for key, value in incoming.items():
        if value is None:
            continue
        if key == "embed_text":
            current = existing.get("embed_text")
            if current and len(current) >= len(value or ""):
                continue
            if not value:
                continue
        existing[key] = value


def _envelope_problem(envelope) -> str | None:
    if not isinstance(envelope, dict):
        return "envelope is not an object"
    missing = [k for k in ("type", "body") if k not in envelope]
    if missing:
        return "envelope missing " + " and ".join(missing)
    if not isinstance(envelope["type"], str) or not isinstance(envelope["body"], dict):
        return "envelope type must be a string and body an object"
    return None


def run(tenant: str, root: Path, writer, rejects_path: Path, reset: bool = False) -> Report:
    report = Report(rejects_path=str(rejects_path))
    group_ids = tenant_subgraphs(tenant)  # also validates the tenant name
    if not Path(root).is_dir():
        raise InputError(f"input folder does not exist: {root}")
    if not input_files(Path(root)):
        raise InputError(f"no *.jsonl files found under {root} (files inside *.parts folders are ignored)")
    lines = list(read_envelopes(Path(root)))  # whole input in memory; fine at capstone scale

    ctx = MapContext(tenant)
    for line in lines:
        if line.error or _envelope_problem(line.envelope) or line.envelope["type"] != "jira":
            continue
        try:
            pair = issue_index(line.envelope["body"])
        except Exception:
            continue  # the same line is rejected with a reason in the main pass
        if pair:
            ctx.jira_issue_keys[pair[0]] = pair[1]

    rejects: list[dict] = []
    nodes: dict[tuple, object] = {}
    edges: dict[tuple, object] = {}

    def reject(line, reason: str) -> None:
        report.rejected += 1
        rejects.append({"file": line.file, "line": line.line, "reason": reason, "record": line.envelope})

    for line in lines:
        report.records += 1
        if line.error:
            reject(line, line.error)
            continue
        problem = _envelope_problem(line.envelope)
        if problem:
            reject(line, problem)
            continue
        source = line.envelope["type"]
        try:
            mapped = map_record(ctx, line.envelope)
            validate_mapped(mapped, source)
        except Skip as skip:
            report.skipped[f"{source}:{skip.kind}"] += 1
            continue
        except Reject as exc:
            reject(line, str(exc))
            continue
        except Exception as exc:  # a malformed body must not abort the load
            reject(line, f"mapper error: {type(exc).__name__}: {exc}")
            continue
        report.mapped += 1
        for node in mapped.nodes:
            existing = nodes.setdefault((node.label, node.uuid), node)
            if existing is not node:
                _merge_props(existing.props, node.props)
        for edge in mapped.edges:
            key = (edge.type, edge.src_label, edge.src_uuid, edge.dst_label, edge.dst_uuid)
            existing = edges.setdefault(key, edge)
            if existing is not edge:
                _merge_props(existing.props, edge.props)

    # Written before the database is touched, so a Neo4j failure still leaves the rejects on disk.
    if rejects:
        Path(rejects_path).parent.mkdir(parents=True, exist_ok=True)
        Path(rejects_path).write_text("\n".join(json.dumps(r) for r in rejects) + "\n", encoding="utf-8")

    if reset and not nodes:
        raise InputError("--reset refused: no nodes were mapped from the input, so nothing would be "
                         "loaded after wiping the tenant; the database was not touched")

    writer.ensure_schema()
    if reset:
        writer.reset(group_ids)
    report.nodes_written = writer.write_nodes(list(nodes.values()))
    report.edges_sent = len(edges)
    report.edges_written = writer.write_edges(list(edges.values()))  # merge hits only
    if report.edges_written < report.edges_sent:
        report.warnings.append(
            f"{report.edges_sent - report.edges_written} edges were not written because an endpoint "
            "is not in the graph (for example a link to a ticket outside the input)")
    report.warnings += sorted(set(ctx.warnings))

    facts = writer.read_facts(group_ids)
    report.node_counts = dict(facts.node_counts)
    report.problems = check_graph(facts)

    return report
