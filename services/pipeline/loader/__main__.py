"""Run the extract and load workers against Postgres, or map and check connector JSONL in memory.

  python -m loader extract [--once]   source_records -> episodes (EMBER-85; see loader/workers.py)
  python -m loader load [--once]      episodes -> the Postgres graph (pg_writer.py)
  python -m loader e2e-check --tenant photon --seed-photon   extract, load in memory, verify (EMBER-86)

  python -m loader --tenant brandon-dev --input data/raw/brandon-dev   (in memory; writes no database)
  python -m loader --tenant brandon-dev --embed [--profile openai-dev]   (also embed each node's text)

Exit code 0 means every record mapped and the graph matches the schema; 1 means
records were rejected, the schema check found mismatches or the embedding step failed; 2 means the run did not
start (input folder missing or without *.jsonl).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from models.client import Client
from models.config import ROOT, ConfigError, load_dotenv, load_profile
from models.identity import identity_for
from ontology import subgraph_id

from .embed import embed_graph
from .memory_writer import MemoryWriter
from .run import InputError, run


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("extract", "load"):
        from .workers import main as worker_main  # needs the database packages; the JSONL loader does not

        return worker_main(argv)
    if argv and argv[0] == "e2e-check":  # EMBER-86: extract + load + verify the graph (loader/e2e_check.py)
        from .e2e_check import main as check_main

        return check_main(argv[1:])
    parser = argparse.ArgumentParser(prog="loader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="tenant name, lowercase letters, digits and '-'")
    parser.add_argument("--input", type=Path, help="folder of *.jsonl (default data/raw/<tenant>)")
    parser.add_argument("--rejects", type=Path, default=Path("rejects.jsonl"))
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

    # Keys come from the environment; .env fills in what is not already set.
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")

    embedder = None
    if args.embed:
        try:
            profile = load_profile(args.profile)
            profile.embedding_key()  # fail now, before any loading
        except ConfigError as exc:
            raise SystemExit(str(exc))
        client, identity = Client(profile), identity_for(profile)

        def embedder(writer, group_ids):
            return embed_graph(writer, client, identity, group_ids)

    root = args.input or Path("data/raw") / args.tenant
    writer = MemoryWriter()
    # run() writes the rejects file only when something was rejected (and creates its parent
    # folders then), so a clean rerun would otherwise leave the previous run's file behind.
    args.rejects.unlink(missing_ok=True)
    try:
        report = run(args.tenant, root, writer, args.rejects, embedder=embedder)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.render())
    if report.embedding_error:
        print(f"error: {report.embedding_error}", file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
