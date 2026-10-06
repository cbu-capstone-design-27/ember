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


def read_envelopes(root: Path) -> Iterator[Line]:
    schema = envelope_validate.load_schema()
    for path in sorted(Path(root).rglob("*.jsonl")):
        # Skip files with .parts directory in their path
        if any(part.endswith(".parts") for part in path.parts):
            continue
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
