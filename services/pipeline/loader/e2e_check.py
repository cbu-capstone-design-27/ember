"""End-to-end check of the Postgres -> Neo4j path (EMBER-86): extract, load, then verify the graph.

    DATABASE_URL=postgresql://... NEO4J_PASSWORD=... \
        python -m loader.e2e_check --tenant photon --seed-photon --reset

One run does this, in order:

1. (--seed-photon) puts the photon Jira and Slack rows into source_records, as apps/mcp/seed_photon.py --no-embed does.
2. (--reset) deletes the tenant's subgraphs in Neo4j and marks the tenant's episodes unloaded, so the run
   loads everything again. Without it the check reads whatever the graph holds now.
3. Runs the extract worker and the load worker once each (`python -m loader extract --once`, then `load --once`).
4. Reads the graph back and compares, per subgraph, the count of every label and every (source label,
   relationship type, target label) with an expected-counts file, then runs the file's sample Cypher
   queries and compares their rows. It also runs the schema check (loader/check.py) on every subgraph.

The expected file: --expected FILE, or fixtures/photon/expected-graph.json for tenant photon, which
fixtures/photon/derive_expected.py derives from the fixtures alone. With no expected file (your own
tenant's data) the check reports the counts, the schema-check mismatches and what extract left behind, and
passes when there are no mismatches. Five smoke queries run instead of the sample ones: same shapes, they print
only counts and pass or fail. The output then holds no message text, names, keys or ids (worker logs are off
unless --verbose).

--memory loads into an in-memory graph instead of Neo4j: counts and the schema check only, no queries.

Exit 0: everything matched. 1: a count, a query or the schema check differs, or episodes are left unloaded.
2: the run did not start (missing DATABASE_URL or NEO4J_PASSWORD, unreadable expected file).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ontology import parse_subgraph_id, tenant_subgraphs

from .check import check_graph
from .facts import GraphFacts

PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parents[1]
PHOTON_EXPECTED = PIPELINE / "fixtures" / "photon" / "expected-graph.json"


class SetupError(Exception):
    """The check cannot start (exit 2)."""


# --- results ---

@dataclass
class CountRow:
    group: str
    what: str  # "node Message" or "edge Message PART_OF Conversation"
    expected: int | None  # None: no expected value (reporting only)
    actual: int

    @property
    def ok(self) -> bool:
        return self.expected is None or self.expected == self.actual


@dataclass
class QueryResult:
    id: str
    question: str
    expected: list
    actual: list | None  # None: not run
    note: str = ""
    smoke: bool = False  # no known answer: `actual` holds counts only and smoke_ok is the verdict
    smoke_ok: bool = False

    @property
    def ok(self) -> bool:
        if self.smoke:
            return self.actual is not None and self.smoke_ok
        return self.actual is not None and self.actual == self.expected

    @property
    def ran(self) -> bool:
        return self.actual is not None


@dataclass
class Result:
    tenant: str
    counts: list[CountRow] = field(default_factory=list)
    problems: dict[str, list[str]] = field(default_factory=dict)  # group -> schema-check mismatches
    queries: list[QueryResult] = field(default_factory=list)
    pipeline: dict = field(default_factory=dict)  # what extract and load did, and what is left over
    has_expected: bool = False

    @property
    def failures(self) -> list[str]:
        out = [f"{row.group}: {row.what}: expected {row.expected}, got {row.actual}" for row in self.counts if not row.ok]
        out += [f"{group}: schema check: {p}" for group, ps in self.problems.items() for p in ps]
        out += [f"query {q.id}: smoke check failed" if q.smoke else f"query {q.id}: expected {q.expected}, got {q.actual}"
                for q in self.queries if q.ran and not q.ok]
        if self.pipeline.get("unloaded"):
            out.append(f"{self.pipeline['unloaded']} episodes of {self.tenant} are still unloaded")
        if self.pipeline.get("rejected"):
            out.append(f"extract rejected {self.pipeline['rejected']} source_records")
        return out

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def render(self) -> str:
        lines = [f"EMBER-86 end-to-end check, tenant {self.tenant}", ""]
        if self.pipeline:
            lines.append("pipeline: " + ", ".join(f"{k}={v}" for k, v in sorted(self.pipeline.items())))
            lines.append("")
        width = max([len(r.what) for r in self.counts] + [10])
        lines.append(f"{'subgraph':<18}{'what':<{width + 2}}{'expected':>9}{'actual':>8}  result")
        for row in self.counts:
            expected = "-" if row.expected is None else str(row.expected)
            lines.append(f"{row.group:<18}{row.what:<{width + 2}}{expected:>9}{row.actual:>8}  "
                         + ("ok" if row.ok else "MISMATCH"))
        if not self.has_expected:
            lines.append("(no expected-counts file: counts are reported, not compared)")
        for group, problems in self.problems.items():
            lines.append(f"{group} schema check: " + ("OK" if not problems else f"{len(problems)} mismatches"))
            lines += [f"  - {p}" for p in problems]
        lines.append("")
        for q in self.queries:
            status = "ok" if q.ok else ("not run: " + q.note if not q.ran else "MISMATCH")
            lines.append(f"query {q.id}: {status}  ({q.question})")
            if q.smoke and q.ran:
                lines.append("  counts " + json.dumps(q.actual[0]))
            elif q.ran and not q.ok:
                lines.append(f"  expected {json.dumps(q.expected)}")
                lines.append(f"  actual   {json.dumps(q.actual)}")
        lines.append("")
        lines.append("RESULT: " + ("PASS" if not self.failures else f"FAIL ({len(self.failures)})"))
        lines += [f"  - {f}" for f in self.failures]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "tenant": self.tenant, "passed": not self.failures, "failures": self.failures, "pipeline": self.pipeline,
            "counts": [{"subgraph": r.group, "what": r.what, "expected": r.expected, "actual": r.actual, "ok": r.ok}
                       for r in self.counts],
            "schema_problems": self.problems,
            "queries": [{"id": q.id, "question": q.question, "smoke": q.smoke, "actual": q.actual, "ok": q.ok,
                         "note": q.note, **({} if q.smoke else {"expected": q.expected})} for q in self.queries],
        }


# --- comparing ---

def fact_counts(facts: GraphFacts) -> dict[str, int]:
    """One flat map: 'node <Label>' and 'edge <Src> <TYPE> <Dst>' -> count."""
    counts = {f"node {label}": n for label, n in facts.node_counts.items()}
    counts.update({f"edge {src} {rel} {dst}": n for (src, rel, dst), n in facts.relationships.items()})
    return counts


def compare_counts(group: str, expected: dict | None, facts: GraphFacts) -> list[CountRow]:
    """Every label and relationship type in the expected file or the graph, one row each."""
    actual = fact_counts(facts)
    if expected is None:
        return [CountRow(group, what, None, n) for what, n in sorted(actual.items())]
    want = {f"node {label}": n for label, n in expected.get("nodes", {}).items()}
    want.update({f"edge {name}": n for name, n in expected.get("relationships", {}).items()})
    return [CountRow(group, what, want.get(what, 0), actual.get(what, 0)) for what in sorted(set(want) | set(actual))]


def _normal(value):
    if isinstance(value, list):
        return sorted((_normal(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in value.items()}
    return value


def _fill(value, tenant: str):
    if isinstance(value, str):
        return value.replace("{tenant}", tenant)
    if isinstance(value, list):
        return [_fill(v, tenant) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, tenant) for k, v in value.items()}
    return value


def run_queries(writer, tenant: str, specs: list[dict]) -> list[QueryResult]:
    """The sample queries against the graph. A writer without Cypher (MemoryWriter) skips them."""
    results = []
    for spec in specs:
        expected = _fill(spec["expect"], tenant)
        if not hasattr(writer, "_run"):
            results.append(QueryResult(spec["id"], spec["question"], expected, None, "needs Neo4j (not --memory)"))
            continue
        params = {"jira": f"{tenant}_jira", "slack": f"{tenant}_slack", "github": f"{tenant}_github",
                  **_fill(spec.get("params", {}), tenant)}
        rows = writer._run(spec["cypher"], **params)
        ordered = bool(spec.get("ordered"))
        actual, expected = _normal_rows(rows, ordered), _normal_rows(expected, ordered)
        results.append(QueryResult(spec["id"], spec["question"], expected, actual))
    return results


# The five query shapes as smoke checks for data without known answers (real data). Each returns only
# counts, so nothing a person wrote can reach the output. `ok` judges the one row of numbers.
SMOKE_QUERIES = [
    {"id": "comment-authors", "question": "who commented on an issue (comments with an author and an issue)",
     "cypher": "MATCH (p:Person {group_id: $jira})-[:AUTHORED]->(m:Message)-[:REFERENCES]->(w:WorkItem {group_id: $jira}) "
               "RETURN count(DISTINCT w) AS issues_with_comments, count(DISTINCT p) AS commenters, count(DISTINCT m) AS comments",
     "ok": lambda r: r["comments"] >= r["issues_with_comments"] and (r["comments"] == 0 or r["commenters"] >= 1)},
    {"id": "thread-messages", "question": "messages in a Slack thread, with their senders",
     "cypher": "MATCH (m:Message {group_id: $slack})-[:PART_OF]->(c:Conversation {group_id: $slack}) "
               "WITH c, count(m) AS n "
               "WITH count(c) AS threads, coalesce(max(n), 0) AS largest_thread, coalesce(sum(n), 0) AS messages "
               "RETURN threads, largest_thread, messages, "
               "count { MATCH (:Identity {group_id: $slack})-[:RESOLVES_TO]->(:Person)-[:AUTHORED]->(x:Message)-[:PART_OF]->(:Conversation) } AS messages_with_sender",
     "ok": lambda r: r["threads"] == 0 or (r["largest_thread"] >= 1 and r["messages"] >= r["threads"]
                                          and r["messages_with_sender"] <= r["messages"])},
    {"id": "issue-references", "question": "issues linked to other issues",
     "cypher": "MATCH (w:WorkItem {group_id: $jira}) "
               "WITH w, count { (w)-[:RELATES_TO]-(:WorkItem) } AS links "
               "RETURN count(w) AS issues, sum(CASE WHEN links > 0 THEN 1 ELSE 0 END) AS linked_issues, "
               "sum(CASE WHEN w.placeholder = true THEN 1 ELSE 0 END) AS placeholders",
     "ok": lambda r: r["linked_issues"] <= r["issues"] and r["placeholders"] <= r["issues"]},
    {"id": "deleted-state", "question": "deleted items keep only their keys: none still holds content",
     "cypher": "MATCH (n) WHERE n.group_id IN [$jira, $slack] AND n.deleted = true "
               "RETURN count(n) AS deleted, sum(CASE WHEN n.embed_text IS NOT NULL OR n.name IS NOT NULL OR n.summary IS NOT NULL "
               "OR n.description IS NOT NULL OR n.embedding IS NOT NULL THEN 1 ELSE 0 END) AS still_holding_content",
     "ok": lambda r: r["still_holding_content"] == 0},
    {"id": "subgraph-isolation", "question": "no edge crosses subgraphs and every node's source matches its subgraph",
     "cypher": "MATCH (n) WHERE n.group_id IN [$jira, $slack] "
               "WITH count(n) AS nodes, sum(CASE WHEN (n.group_id = $jira AND n.source <> 'jira') "
               "OR (n.group_id = $slack AND n.source <> 'slack') THEN 1 ELSE 0 END) AS wrong_source "
               "CALL { MATCH (a)-[r]->(b) WHERE a.group_id IN [$jira, $slack] AND a.group_id <> b.group_id RETURN count(r) AS crossing } "
               "RETURN nodes, wrong_source, crossing",
     "ok": lambda r: r["wrong_source"] == 0 and r["crossing"] == 0},
]


def run_smoke_queries(writer, tenant: str) -> list[QueryResult]:
    """Real-data mode: run the five shapes, report only the numbers they return and pass or fail."""
    results = []
    for spec in SMOKE_QUERIES:
        if not hasattr(writer, "_run"):
            results.append(QueryResult(spec["id"], spec["question"], [], None, "needs Neo4j (not --memory)", smoke=True))
            continue
        rows = writer._run(spec["cypher"], jira=f"{tenant}_jira", slack=f"{tenant}_slack")
        row = {k: (v or 0) for k, v in (rows[0] if rows else {}).items()}
        results.append(QueryResult(spec["id"], spec["question"], [], [row], smoke=True,
                                   smoke_ok=bool(row) and bool(spec["ok"](row))))
    return results


def _normal_rows(rows: list, ordered: bool = False) -> list:
    rows = [_normal(r) for r in rows]
    return rows if ordered else sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


# --- the run ---

def load_expected(path: Path | None, tenant: str) -> dict | None:
    if path is None and tenant == "photon" and PHOTON_EXPECTED.exists():
        path = PHOTON_EXPECTED
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"cannot read the expected-counts file {path}: {exc}") from exc


async def seed_photon(database_url: str) -> None:
    spec = importlib.util.spec_from_file_location("seed_photon", REPO / "apps" / "mcp" / "seed_photon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    os.environ["DATABASE_URL"] = database_url
    await module.main(embed_rows=False)


async def run_pipeline(database_url: str, writer, tenant: str, reset: bool) -> dict:
    """Reset (optional), then extract and load once each. Returns what happened and what is left."""
    import asyncpg

    from .workers import ExtractWorker, LoadWorker

    if reset:
        writer.reset(tenant_subgraphs(tenant))
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute("update episodes set loaded_at = null where tenant = $1", tenant)
        finally:
            await conn.close()
    extract = ExtractWorker(database_url)
    await extract.drain()
    load = LoadWorker(database_url, writer)
    await load.drain()

    conn = await asyncpg.connect(database_url)
    try:
        stored = await conn.fetch("select source, kind, count(*) as n from source_records where tenant = $1 "
                                  "group by 1, 2 order by 1, 2", tenant)
        unloaded = await conn.fetchval("select count(*) from episodes where tenant = $1 and loaded_at is null", tenant)
        episodes = await conn.fetchval("select count(*) from episodes where tenant = $1", tenant)
    finally:
        await conn.close()
    info = {f"source_records {r['source']}/{r['kind']}": r["n"] for r in stored}
    info.update({"episodes": episodes, "unloaded": unloaded,
                 "extracted_now": extract.counts["extracted"], "loaded_now": load.counts["loaded"],
                 "rejected": extract.counts["rejected"], "skipped": extract.counts["skipped"],
                 "load_failed": load.counts["failed"]})
    # By record kind, as counts only: never the records, their ids or the reasons
    for key, n in extract.counts.items():
        parts = key.split(":")
        if len(parts) == 3 and parts[0] in ("rejected", "skipped", "not_extracted"):
            info[f"{parts[0].replace('_', ' ')} {parts[1]}/{parts[2]}"] = n
    return info


def verify(writer, tenant: str, expected: dict | None) -> Result:
    """Compare the graph the writer holds with the expected file (or just report it)."""
    result = Result(tenant, has_expected=expected is not None)
    for group in tenant_subgraphs(tenant):
        source = parse_subgraph_id(group)[1]
        facts = writer.read_facts([group])
        want = expected["subgraphs"].get(source) if expected else None
        if not facts.node_counts and not want:
            continue  # nothing loaded and nothing expected: a source this tenant does not have
        result.counts += compare_counts(group, want, facts)
        result.problems[group] = check_graph(facts)
    if expected:
        result.queries = run_queries(writer, tenant, expected.get("queries", []))
    else:
        result.queries = run_smoke_queries(writer, tenant)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m loader.e2e_check", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="tenant whose source_records are loaded and checked")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                        help="Postgres holding source_records (default: DATABASE_URL)")
    parser.add_argument("--neo4j-uri", help="default: NEO4J_URI or bolt://localhost:7687 (user NEO4J_USER, password NEO4J_PASSWORD)")
    parser.add_argument("--expected", type=Path, help="expected counts and queries (default for photon: the fixtures')")
    parser.add_argument("--seed-photon", action="store_true", help="first put the photon fixtures into source_records (tenant photon)")
    parser.add_argument("--reset", action="store_true", help="delete the tenant's subgraphs and reload every episode first")
    parser.add_argument("--memory", action="store_true", help="load into memory, not Neo4j: counts and schema check only")
    parser.add_argument("--verbose", action="store_true",
                        help="show the workers' log lines (record ids and rejection reasons: may quote real data)")
    parser.add_argument("--json", type=Path, help="also write the result here as JSON")
    args = parser.parse_args(argv)
    # The workers log record ids and rejection reasons, which can quote the data: silent unless --verbose
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.CRITICAL)
    for name in ("neo4j", "neo4j.notifications"):
        logging.getLogger(name).setLevel(logging.ERROR if args.verbose else logging.CRITICAL)

    try:
        if not args.database_url:
            raise SetupError("set DATABASE_URL or pass --database-url (the Postgres that holds source_records)")
        if args.seed_photon and args.tenant != "photon":
            raise SetupError("--seed-photon loads the photon fixtures, tenant photon")
        expected = load_expected(args.expected, args.tenant)
        if args.memory:
            from .memory_writer import MemoryWriter

            writer = MemoryWriter()
        else:
            from .neo4j_writer import Neo4jWriter

            password = os.environ.get("NEO4J_PASSWORD")
            if not password:
                raise SetupError("NEO4J_PASSWORD is not set (or use --memory)")
            writer = Neo4jWriter(args.neo4j_uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                                 os.environ.get("NEO4J_USER", "neo4j"), password,
                                 database=os.environ.get("NEO4J_DATABASE") or None)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    async def go() -> dict:
        if args.seed_photon:
            await seed_photon(args.database_url)
        # an in-memory graph starts empty, so every episode has to be loaded into it again
        return await run_pipeline(args.database_url, writer, args.tenant, args.reset or args.memory)

    try:
        pipeline = asyncio.run(go())
        result = verify(writer, args.tenant, expected)
        result.pipeline = pipeline
    finally:
        if hasattr(writer, "close"):
            writer.close()
    print(result.render())
    if args.json:
        args.json.write_text(json.dumps(result.to_json(), indent=2, default=str) + "\n", encoding="utf-8")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
