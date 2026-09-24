"""Validate ingestion envelopes against schema/envelope.v1.schema.json.

Stdlib only. Covers the JSON Schema subset this contract uses:
type, enum, const, required, properties, additionalProperties, items,
minLength, minimum, format date-time, allOf, oneOf, if/then.
"""

from __future__ import annotations

import json
from datetime import datetime
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


def _date_time_ok(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")

    expected = schema.get("type")
    if expected is not None and not _type_ok(instance, expected):
        errors.append(f"{path}: expected {expected}")
        return errors

    if expected == "string":
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if schema.get("format") == "date-time" and not _date_time_ok(instance):
            errors.append(f"{path}: invalid date-time")

    if expected == "integer" and "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: below minimum {schema['minimum']}")

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

    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))

    for sub in schema.get("allOf", []):
        errors.extend(validate(instance, sub, path))

    if "oneOf" in schema:
        branches = schema["oneOf"]
        matched = sum(1 for branch in branches if not validate(instance, branch, path))
        if matched != 1:
            errors.append(f"{path}: expected exactly one matching schema, got {matched}")

    if "if" in schema:
        if not validate(instance, schema["if"], path):
            then_schema = schema.get("then")
            if then_schema is not None:
                errors.extend(validate(instance, then_schema, path))

    return errors


def assert_valid(instance, schema: dict | None = None) -> None:
    schema = load_schema() if schema is None else schema
    errors = validate(instance, schema)
    if errors:
        raise AssertionError("\n".join(errors))
