"""Run the benchmark suites.

  python -m benchmarks run  --tenant brandon-dev --input ../../data/raw/brandon-dev [--suite all] [--neo4j]
  python -m benchmarks docs --tenant brandon-dev --input ../../data/raw/brandon-dev
  python -m benchmarks docs --tenant brandon-dev --neo4j

`run` loads the raw data with the loader (in memory, or into Neo4j with --neo4j, which resets the tenant first),
embeds it when the retrieval suite is selected, runs the suites, prints a summary and saves JSON under
benchmarks/results/. Retrieval scores are development-model scores and say which models produced them.
`docs` lists every embeddable node (ref and text) to help write questions. It never resets or writes Neo4j:
without --neo4j it loads --input in memory; with --neo4j it only reads the tenant's existing graph.

The spec and questions files are read and checked before anything is reset, loaded or sent to a model.

Exit code 0 means the suites ran on a clean load; 1 means a configuration, model, embedding or benchmark error,
or that the load was not clean (rejected records, schema problems or a failed loading check; the result is still
saved); 2 means an input error (bad tenant, missing or empty input folder, missing or malformed spec or questions
file, answers that are not nodes in the graph).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from loader.embed import embed_graph
from loader.memory_writer import MemoryWriter
from loader.reader import input_files
from loader.run import InputError
from loader.run import run as run_loader
from models.client import Client, ModelError
from models.config import ROOT, ConfigError, load_dotenv, load_profile
from models.identity import identity_for
from ontology import tenant_subgraphs
from ontology.config import EmbeddingMismatch

from .loading import run_loading, score_loading
from .results import BenchmarkError, build_meta, render_summary, save_result
from .retrieval_suite import docs_from, load_questions, run_retrieval

HERE = Path(__file__).parent
DATA, RESULTS = HERE / "data", HERE / "results"


def _ref(candidate: dict) -> str:
    return f"{candidate['group_id'].rsplit('_', 1)[1]}:{candidate['key']}"


def _read_json(path: Path, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read the {what} file {path}: {exc.strerror or exc}") from None
    except ValueError as exc:  # bad JSON and bad UTF-8
        raise InputError(f"the {what} file {path} is not valid JSON: {exc}") from None


def load_spec(path: Path) -> dict:
    """Read the loading expectations and check their shape, so a typo fails before anything is loaded."""
    spec = _read_json(path, "loading spec")

    def bad(problem: str):
        raise InputError(f"the loading spec file {path} is malformed: {problem}")

    if not isinstance(spec, dict):
        bad("the top level must be an object")
    rate = spec.get("max_reject_rate", 0.0)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        bad("max_reject_rate must be a number")
    nodes = spec.get("nodes", {})
    if not (isinstance(nodes, dict) and all(isinstance(v, dict) and all(
            isinstance(n, int) and not isinstance(n, bool) for n in v.values()) for v in nodes.values())):
        bad("nodes must map source -> label -> count")
    fields = spec.get("fields", [])
    if not (isinstance(fields, list) and all(
            isinstance(f, dict) and isinstance(f.get("ref"), str) and ":" in f["ref"]
            and isinstance(f.get("label"), str) and isinstance(f.get("props"), dict) for f in fields)):
        bad("each fields entry needs ref (source:key), label and props")
    edges = spec.get("edges", [])
    keys = ("type", "src_label", "src", "dst_label", "dst")
    if not (isinstance(edges, list) and all(
            isinstance(e, dict) and all(isinstance(e.get(k), str) for k in keys) and ":" in e["src"]
            for e in edges)):
        bad("each edges entry needs type, src_label, src, dst_label and dst")
    return spec


def read_questions(path: Path):
    """Read and check the questions file; any problem is an input error (exit 2)."""
    _read_json(path, "questions")  # file and JSON problems get their own message
    try:
        return load_questions(path)
    except BenchmarkError as exc:
        raise InputError(str(exc)) from None
    except (AttributeError, TypeError):
        raise InputError(f"the questions file {path} is malformed: expected an object with a questions list "
                         "of {id, question, answers} objects") from None


def _check_answers(questions, candidates: list[dict], path: Path) -> None:
    """Before any paid embedding: every expected answer must be a node that exists in the graph."""
    known = {_ref(c) for c in candidates}
    unknown = sorted({a for q in questions for a in q.answers if a not in known})
    if unknown:
        raise InputError(f"{path}: answers not found among the graph's embeddable nodes "
                         f"(try `python -m benchmarks docs`): {', '.join(unknown)}")


def _check_input(root: Path) -> None:
    """The loader's own input checks, done first so a bad input folder changes nothing (not even rejects.jsonl)."""
    if not Path(root).is_dir():
        raise InputError(f"input folder does not exist: {root}")
    if not input_files(Path(root)):
        raise InputError(f"no *.jsonl files found under {root} (files inside *.parts folders are ignored)")


def run_suites(suites, *, tenant, report, writer, loading_spec, questions_file, embed_queries, profile, out_dir):
    """Run the chosen suites against a loaded (and, for retrieval, embedded) graph; save and return one result each."""
    group_ids = tenant_subgraphs(tenant)
    # A result file must show whether the load behind it was clean.
    load = {"records": report.records, "rejected": report.rejected, "problems": len(report.problems)}
    results = []
    if "loading" in suites:
        spec = load_spec(loading_spec)
        checks = run_loading(spec, report, writer.snapshot(group_ids))
        results.append({"suite": "loading", "meta": {**build_meta(tenant, Path(loading_spec).name), **load},
                        "scores": score_loading(checks),
                        "checks": [{"group": c.group, "name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]})
    if "retrieval" in suites:
        meta, questions = read_questions(questions_file)
        questions_hash = hashlib.sha256(Path(questions_file).read_bytes()).hexdigest()
        docs = docs_from(writer.embed_candidates(group_ids))
        outcome = run_retrieval(questions, docs, embed_queries)
        results.append({"suite": "retrieval",
                        "meta": {**build_meta(tenant, meta.get("dataset", Path(questions_file).name), profile, docs,
                                              extra=questions_hash),
                                 **load, "questions": len(questions), "documents": len(docs)},
                        "scores": outcome["scores"], "details": outcome["details"]})
    for result in results:
        save_result(result, out_dir)
    return results


def _failures(report, results) -> list[str]:
    failures = []
    if report.rejected:
        failures.append(f"{report.rejected} records were rejected (see {report.rejects_path})")
    if report.problems:
        failures.append(f"the schema check found {len(report.problems)} problems")
    failed = sum(not c["ok"] for r in results if r["suite"] == "loading" for c in r["checks"])
    if failed:
        failures.append(f"{failed} loading checks failed")
    return failures


def _writer(args):
    if not args.neo4j:
        return MemoryWriter()
    from loader.neo4j_writer import Neo4jWriter

    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        raise SystemExit("NEO4J_PASSWORD is not set (put it in .env); see .env.example")
    return Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                       os.environ.get("NEO4J_USER", "neo4j"), password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "docs"])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--input", type=Path, help="folder of raw connector JSONL (not used by docs --neo4j)")
    parser.add_argument("--suite", choices=["loading", "retrieval", "all"], default="all")
    parser.add_argument("--spec", type=Path, help="loading expectations (default data/loading_<tenant>.json)")
    parser.add_argument("--questions", type=Path, help="retrieval questions (default data/questions_<tenant>.json)")
    parser.add_argument("--profile", help="model profile (default: MODEL_PROFILE)")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--rejects", type=Path, default=Path("rejects.jsonl"))
    parser.add_argument("--neo4j", action="store_true",
                        help="run: load into Neo4j (the tenant is reset) instead of memory; "
                             "docs: read the tenant's existing Neo4j graph, never reset or load")
    parser.add_argument("--no-dotenv", action="store_true")
    args = parser.parse_args(argv)
    reads_existing_graph = args.command == "docs" and args.neo4j
    if args.input is None and not reads_existing_graph:
        parser.error("--input is required (except for docs --neo4j)")
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")

    suites = ["loading", "retrieval"] if args.suite == "all" else [args.suite]
    needs_models = args.command == "run" and "retrieval" in suites
    profile = client = writer = None
    try:
        try:
            group_ids = tenant_subgraphs(args.tenant)
        except ValueError as exc:
            raise InputError(str(exc)) from None
        spec_path = args.spec or DATA / f"loading_{args.tenant}.json"
        questions_path = args.questions or DATA / f"questions_{args.tenant}.json"
        questions = []
        if args.command == "run":
            # Everything that can be checked for free is checked before any reset, load or API call.
            if "loading" in suites:
                load_spec(spec_path)
            if "retrieval" in suites:
                _, questions = read_questions(questions_path)
        if needs_models:
            profile = load_profile(args.profile)
            profile.embedding_key()  # fail now, before any loading or Neo4j connection
            client = Client(profile)
        if not reads_existing_graph:
            _check_input(args.input)
        writer = _writer(args)

        if reads_existing_graph:
            report = None
        else:
            args.rejects.unlink(missing_ok=True)  # input is known good; the loader writes the file only on rejects
            report = run_loader(args.tenant, args.input, writer, args.rejects, reset=args.neo4j)

        if args.command == "docs":
            for c in sorted(writer.embed_candidates(group_ids), key=lambda c: (c["group_id"], c["key"])):
                print(f"{_ref(c)}\t{' '.join(c['text'].split())[:100]}")
            return 0

        if needs_models:
            _check_answers(questions, writer.embed_candidates(group_ids), questions_path)
            embed_graph(writer, client, identity_for(profile), group_ids)
        results = run_suites(suites, tenant=args.tenant, report=report, writer=writer,
                             loading_spec=spec_path, questions_file=questions_path,
                             embed_queries=client.embed if client else None, profile=profile, out_dir=args.out)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, ModelError, EmbeddingMismatch, BenchmarkError, FileNotFoundError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if hasattr(writer, "close"):
            writer.close()
    print("\n\n".join(render_summary(r) for r in results))
    failures = _failures(report, results)
    if failures:
        print("benchmark failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
