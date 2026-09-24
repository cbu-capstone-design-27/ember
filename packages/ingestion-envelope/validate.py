"""Validate ingestion intake records against schema/envelope.v1.schema.json.

Stdlib only. Covers the JSON Schema subset this contract uses:
type, enum, required, properties, additionalProperties.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema" / "envelope.v1.schema.json"
FIXTURES_DIR = ROOT / "fixtures"
SOURCES = ("github", "jira", "slack", "teams", "gitlab")


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_fixtures() -> dict[str, dict]:
    found = {}
    for source in SOURCES:
        path = FIXTURES_DIR / f"{source}.json"
        found[source] = json.loads(path.read_text(encoding="utf-8"))
    return found


def _type_ok(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported type: {expected}")


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []

    expected = schema.get("type")
    if expected is not None and not _type_ok(instance, expected):
        errors.append(f"{path}: expected {expected}")
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")

    if isinstance(instance, dict) and (
        "properties" in schema or "required" in schema or "additionalProperties" in schema
    ):
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key in required:
            if key not in instance:
                errors.append(f"{path}.{key}: missing")
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key}: additional property")

    return errors


def assert_valid(instance, schema: dict | None = None) -> None:
    schema = load_schema() if schema is None else schema
    errors = validate(instance, schema)
    if errors:
        raise AssertionError("\n".join(errors))
