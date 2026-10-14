"""Read connector JSONL and check each line against the envelope schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "ingestion-envelope"))

import validate as envelope_validate  # noqa: E402


class Line(NamedTuple):
    file: str
    line: int
    envelope: dict | None
    error: str | None


def input_files(root: Path) -> list[Path]:
    """The *.jsonl files under root, minus anything inside a *.parts directory.

    Only directory names below root count, so a root that itself lives under a folder
    called something.parts still loads.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.jsonl")
                  if path.is_file() and not any(part.endswith(".parts") for part in path.relative_to(root).parts[:-1]))


def read_envelopes(root: Path) -> Iterator[Line]:
    schema = envelope_validate.load_schema()
    for path in input_files(root):
        with path.open(encoding="utf-8") as handle:
            for number, text in enumerate(handle, 1):
                if not text.strip():
                    continue
                try:
                    envelope = json.loads(text)
                except json.JSONDecodeError as exc:
                    yield Line(str(path), number, None, f"invalid JSON: {exc.msg}")
                    continue
                errors = envelope_validate.validate(envelope, schema)
                if errors:
                    yield Line(str(path), number, None, "; ".join(errors))
                else:
                    yield Line(str(path), number, envelope, None)
