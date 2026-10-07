"""Check that the configured models answer.

  python -m models smoke [--profile openai-dev]

Makes one chat call and one embedding call (a few cents at most) and prints the model names and the
vector size. Keys come from the environment or the repo's .env.
"""

from __future__ import annotations

import argparse
import sys

from .client import Client, ModelError
from .config import ROOT, ConfigError, load_dotenv, load_profile
from .smoke import smoke


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="models", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["smoke"])
    parser.add_argument("--profile", help="profile name (default: MODEL_PROFILE or the file's default)")
    parser.add_argument("--no-dotenv", action="store_true", help="do not read the repo's .env")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    try:
        profile = load_profile(args.profile)
        print("\n".join(smoke(profile, Client(profile))))
    except (ConfigError, ModelError) as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
