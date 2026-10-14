"""Load connector JSONL into Neo4j.

  python -m loader --tenant brandon-dev --input data/raw/brandon-dev
  python -m loader --tenant brandon-dev --input data/raw/brandon-dev --dry-run   (no Neo4j)
  python -m loader --tenant brandon-dev --embed [--profile openai-dev]   (also embed each node's text)

Exit code 0 means every record loaded and the graph matches the schema; 1 means
records were rejected, the schema check found mismatches or the embedding step failed; 2 means the run did not
start (input folder missing or without *.jsonl, or --reset with nothing mapped).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from models.client import Client
from models.config import ROOT, ConfigError, load_dotenv, load_profile
from models.identity import identity_for
from ontology import subgraph_id

from .embed import embed_graph
from .memory_writer import MemoryWriter
from .run import InputError, run


def _neo4j_writer():
    from .neo4j_writer import Neo4jWriter

    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("NEO4J_PASSWORD is not set (see .env.example)")
    return Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                       os.environ.get("NEO4J_USER", "neo4j"), password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="tenant name, lowercase letters, digits and '-'")
    parser.add_argument("--input", type=Path, help="folder of *.jsonl (default data/raw/<tenant>)")
    parser.add_argument("--rejects", type=Path, default=Path("rejects.jsonl"))
    parser.add_argument("--dry-run", action="store_true", help="map and check in memory, do not touch Neo4j")
    parser.add_argument("--reset", action="store_true", help="delete this tenant's subgraphs before loading")
    parser.add_argument("--embed", action="store_true", help="embed each node's text with the configured model")
    parser.add_argument("--profile", help="model profile for --embed (default: MODEL_PROFILE)")
    parser.add_argument("--no-dotenv", action="store_true", help="do not read the repo's .env")
    args = parser.parse_args(argv)
    if args.profile and not args.embed:
        parser.error("--profile only applies with --embed")

    try:
        subgraph_id(args.tenant, "github")
    except ValueError as exc:
        raise SystemExit(str(exc))

    # Keys and the Neo4j password come from the environment; .env fills in what is not already set.
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")

    embedder = None
    if args.embed:
        try:
            profile = load_profile(args.profile)
            profile.embedding_key()  # fail now, before any loading or Neo4j connection
        except ConfigError as exc:
            raise SystemExit(str(exc))
        client, identity = Client(profile), identity_for(profile)

        def embedder(writer, group_ids):
            return embed_graph(writer, client, identity, group_ids)

    root = args.input or Path("data/raw") / args.tenant
    writer = MemoryWriter() if args.dry_run else _neo4j_writer()
    # run() writes the rejects file only when something was rejected (and creates its parent
    # folders then), so a clean rerun would otherwise leave the previous run's file behind.
    args.rejects.unlink(missing_ok=True)
    try:
        report = run(args.tenant, root, writer, args.rejects, reset=args.reset, embedder=embedder)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if hasattr(writer, "close"):
            writer.close()
    print(report.render())
    if report.embedding_error:
        print(f"error: {report.embedding_error}", file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
