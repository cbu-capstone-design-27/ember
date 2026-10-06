"""Load connector JSONL into Neo4j.

  python -m loader --tenant brandon-dev --input data/raw/brandon-dev
  python -m loader --tenant brandon-dev --input data/raw/brandon-dev --dry-run   (no Neo4j)

Exit code 0 means every record loaded and the graph matches the schema; 1 means
records were rejected or the schema check found mismatches.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ontology import subgraph_id

from .memory_writer import MemoryWriter
from .run import run


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
    args = parser.parse_args(argv)

    try:
        subgraph_id(args.tenant, "github")
    except ValueError as exc:
        raise SystemExit(str(exc))

    root = args.input or Path("data/raw") / args.tenant
    writer = MemoryWriter() if args.dry_run else _neo4j_writer()
    # run() writes the rejects file only when something was rejected (and creates its parent
    # folders then), so a clean rerun would otherwise leave the previous run's file behind.
    args.rejects.unlink(missing_ok=True)
    try:
        report = run(args.tenant, root, writer, args.rejects, reset=args.reset)
    finally:
        if hasattr(writer, "close"):
            writer.close()
    print(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
