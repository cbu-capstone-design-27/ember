"""Run the extract and load workers against Postgres, or map and check connector JSONL in memory.

  python -m loader extract [--once]   source_records -> episodes (EMBER-85; see loader/workers.py)
  python -m loader load [--once]      episodes -> the Postgres graph (pg_writer.py)
  python -m loader e2e-check --tenant photon --seed-photon   extract, load in memory, verify (EMBER-86)

  python -m loader --tenant brandon-dev --input data/raw/brandon-dev   (in memory; writes no database)

Exit code 0 means every record mapped and the graph matches the schema; 1 means
records were rejected or the schema check found mismatches; 2 means the run did not
start (input folder missing or without *.jsonl).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ontology import subgraph_id

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
    args = parser.parse_args(argv)

    try:
        subgraph_id(args.tenant, "github")
    except ValueError as exc:
        raise SystemExit(str(exc))

    root = args.input or Path("data/raw") / args.tenant
    writer = MemoryWriter()
    # run() writes the rejects file only when something was rejected (and creates its parent
    # folders then), so a clean rerun would otherwise leave the previous run's file behind.
    args.rejects.unlink(missing_ok=True)
    try:
        report = run(args.tenant, root, writer, args.rejects)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
