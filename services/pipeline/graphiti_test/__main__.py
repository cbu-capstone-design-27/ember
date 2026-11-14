"""Stock Graphiti on Neo4j with the photon data (EMBER-146). From services/pipeline:

    python -m graphiti_test run --tenant photon --limit 40 --fake --out ../../data/out/graphiti-fake
    python -m graphiti_test run --tenant photon --limit 40 --profile local-spark --out ../../data/out/graphiti-spark
    python -m graphiti_test diff ../../data/out/a/reference.json ../../data/out/b/reference.json

Reads NEO4J_URI (default bolt://localhost:7687), NEO4J_USER, NEO4J_PASSWORD and NEO4J_DATABASE, plus the key variables
the profile names. Rerun with the same --out to resume. Exit 0 clean, 1 failures (or differences, for diff), 2 could
not start. Steps and caveats: docs/research/graphiti-test.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from models.config import ConfigError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m graphiti_test")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="ingest, search and write a reference")
    run.add_argument("--tenant", default="photon", choices=["photon"], help="only the made-up photon data")
    run.add_argument("--limit", type=int, default=None, help="a sample of N records; default all")
    provider = run.add_mutually_exclusive_group()
    provider.add_argument("--profile", default=None, help="a profile in config/models.toml")
    provider.add_argument("--vertex", action="store_true", help="native Vertex AI with Google application default credentials")
    run.add_argument("--fake", action="store_true", help="use the local fake models; output is not meaningful for "
                                                         "quality or cost")
    run.add_argument("--out", required=True, help="folder for the ledger and the reference; reuse it to resume")
    run.add_argument("--neo4j-uri", default=None)
    run.add_argument("--reset", action="store_true", help="delete this tenant's subgraphs and the ledger first")
    run.add_argument("--concurrency", type=int, default=4, help="Graphiti's max_coroutines, per gather")
    run.add_argument("--structured-mode", default="json_schema", choices=["json_schema", "json_object"],
                     help="how OpenAIGenericClient asks for structured output")
    run.add_argument("--no-resend-check", action="store_true", help="skip re-adding one episode at the end")
    run.add_argument("--verbose", action="store_true")

    diff = commands.add_parser("diff", help="compare two references' comparable halves")
    diff.add_argument("first")
    diff.add_argument("second")
    diff.add_argument("--ranking", action="store_true", help="also list differences in result order")

    args = parser.parse_args(argv)
    if args.command == "run" and args.vertex and args.fake:
        parser.error("--vertex and --fake cannot be combined; use the existing fake profile for offline checks")
    if args.command == "diff":
        from .report import compare, split_ranking

        first, second = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.first, args.second))
        if first.get("fake_models") != second.get("fake_models"):
            print("note: one of these used fake models", file=sys.stderr)
        hard, ranking = split_ranking(compare(first, second))
        print("\n".join(hard[:200]) if hard else "no differences beyond result order")
        if len(hard) > 200:
            print(f"... and {len(hard) - 200} more")
        print(f"{len(ranking)} differences in result order within a search"
              + ("" if args.ranking else " (--ranking lists them)"))
        if args.ranking:
            print("\n".join(ranking))
        return 1 if hard else 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.verbose:  # request logs would drown the progress lines
        for noisy in ("httpx", "httpcore", "openai", "neo4j", "graphiti_core"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    from .run import SetupError, run as execute

    try:
        return asyncio.run(execute(args))
    except (SetupError, ConfigError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
