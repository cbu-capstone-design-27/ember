"""Run the benchmark suites.

  python -m benchmarks run  --tenant brandon-dev --input ../../data/raw/brandon-dev [--suite all] [--neo4j]
  python -m benchmarks docs --tenant brandon-dev --input ../../data/raw/brandon-dev

`run` loads the raw data with the loader (in memory, or into Neo4j with --neo4j, which resets the tenant first),
embeds it when the retrieval suite is selected, runs the suites, prints a summary and saves JSON under
benchmarks/results/. Retrieval scores are development-model scores and say which models produced them.
`docs` lists every embeddable node (ref and text) to help write questions.

Exit code 0 means the suites ran; 1 means a configuration, model, embedding or benchmark-data error;
2 means the loader could not start (input folder missing or empty, or --neo4j with nothing mapped).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from loader.embed import embed_graph
from loader.memory_writer import MemoryWriter
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


def run_suites(suites, *, tenant, report, writer, loading_spec, questions_file, embed_queries, profile, out_dir):
    """Run the chosen suites against a loaded (and, for retrieval, embedded) graph; save and return one result each."""
    group_ids = tenant_subgraphs(tenant)
    results = []
    if "loading" in suites:
        spec = json.loads(Path(loading_spec).read_text(encoding="utf-8"))
        checks = run_loading(spec, report, writer.snapshot(group_ids))
        results.append({"suite": "loading", "meta": build_meta(tenant, Path(loading_spec).name),
                        "scores": score_loading(checks),
                        "checks": [{"group": c.group, "name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]})
    if "retrieval" in suites:
        meta, questions = load_questions(questions_file)
        docs = docs_from(writer.embed_candidates(group_ids))
        outcome = run_retrieval(questions, docs, embed_queries)
        results.append({"suite": "retrieval",
                        "meta": {**build_meta(tenant, meta.get("dataset", Path(questions_file).name), profile, docs),
                                 "questions": len(questions), "documents": len(docs)},
                        "scores": outcome["scores"], "details": outcome["details"]})
    for result in results:
        save_result(result, out_dir)
    return results


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
    parser.add_argument("--input", required=True, type=Path, help="folder of raw connector JSONL")
    parser.add_argument("--suite", choices=["loading", "retrieval", "all"], default="all")
    parser.add_argument("--spec", type=Path, help="loading expectations (default data/loading_<tenant>.json)")
    parser.add_argument("--questions", type=Path, help="retrieval questions (default data/questions_<tenant>.json)")
    parser.add_argument("--profile", help="model profile (default: MODEL_PROFILE)")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--rejects", type=Path, default=Path("rejects.jsonl"))
    parser.add_argument("--neo4j", action="store_true", help="load into Neo4j (the tenant is reset) instead of memory")
    parser.add_argument("--no-dotenv", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")

    suites = ["loading", "retrieval"] if args.suite == "all" else [args.suite]
    needs_models = args.command == "run" and "retrieval" in suites
    profile = client = writer = None
    try:
        if needs_models:
            profile = load_profile(args.profile)
            profile.embedding_key()  # fail now, before any loading or Neo4j connection
            client = Client(profile)
        writer = _writer(args)
        args.rejects.unlink(missing_ok=True)  # the loader writes the file only when something is rejected
        report = run_loader(args.tenant, args.input, writer, args.rejects, reset=args.neo4j)
        group_ids = tenant_subgraphs(args.tenant)

        if args.command == "docs":
            for c in sorted(writer.embed_candidates(group_ids), key=lambda c: (c["group_id"], c["key"])):
                print(f"{c['group_id'].rsplit('_', 1)[1]}:{c['key']}\t{' '.join(c['text'].split())[:100]}")
            return 0

        if needs_models:
            embed_graph(writer, client, identity_for(profile), group_ids)
        results = run_suites(suites, tenant=args.tenant, report=report, writer=writer,
                             loading_spec=args.spec or DATA / f"loading_{args.tenant}.json",
                             questions_file=args.questions or DATA / f"questions_{args.tenant}.json",
                             embed_queries=client.embed if client else None, profile=profile, out_dir=args.out)
    except InputError as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, ModelError, EmbeddingMismatch, BenchmarkError, FileNotFoundError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if hasattr(writer, "close"):
            writer.close()
    print("\n\n".join(render_summary(r) for r in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
