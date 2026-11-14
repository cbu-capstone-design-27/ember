"""python -m ember run | check

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
        GITHUB_WEBHOOK_SECRET=... python -m ember run

`run` starts every configured component and serves HTTP on LISTEN_PORT (8080). `check` prints, as
JSON, what `run` would start and why each other part is disabled, and connects to nothing.
Exits 2 when DATABASE_URL is missing or invalid.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from ember.connectors import CONNECTORS
from ember.core.errors import ConfigError
from ember.core.settings import Settings
from ember.runtime.controller import Controller, plan
from ember.runtime.deps import Deps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ember", description="Ember's backend: intake and the workers")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "check"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.load()
    except ConfigError as error:
        hint = (". DATABASE_URL looks like postgresql://ember:...@localhost:5432/ember?sslmode=disable"
                if "DATABASE_URL" in str(error) else "")
        print(f"{error}{hint}", file=sys.stderr)
        return 2
    if args.command == "check":
        built = plan(Deps.build(settings), CONNECTORS)
        print(json.dumps({"components": [c.name for c in built.components], "disabled": built.disabled}, indent=2))
        return 0
    return asyncio.run(Controller(settings, CONNECTORS).run())


if __name__ == "__main__":
    sys.exit(main())
