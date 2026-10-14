"""What intake needs from each source (docs/ingestion/architecture.md, "Source adapters").

An adapter is everything source-specific about receiving one delivery. Intake does the rest once
for every source: the body limit, storage, health and metrics. Adding a source means adding an
adapter here and to the list in intake.py's main, not a new listener.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Literal, Protocol

from starlette.responses import Response

Source = Literal["github", "gitlab", "jira", "slack", "teams"]  # matches envelope v1 "type"


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


class Unauthorized(Exception):
    """The signature, token or clientState check failed: 401."""


class BadRequest(Exception):
    """The delivery is missing something every delivery from this source has: 400."""


@dataclass(frozen=True)
class Delivery:
    """One request as it arrived. Header names are case-insensitive (Starlette's Headers)."""

    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes

    @cached_property
    def json(self) -> dict:
        """The parsed body. Every source sends a JSON object, and the envelope stores it unchanged."""
        try:
            payload = json.loads(self.body, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:  # bad UTF-8 or JSON, NaN, a huge integer, deep nesting
            raise BadRequest("invalid json") from exc
        if not isinstance(payload, dict):
            raise BadRequest("body must be a JSON object")
        return payload


class SourceAdapter(Protocol):
    name: Source

    def handshake(self, delivery: Delivery) -> Response | None: ...  # Slack, Teams; None for a normal delivery
    def verify(self, delivery: Delivery) -> None: ...  # raises Unauthorized
    def delivery_id(self, delivery: Delivery) -> str: ...  # stable across the source's retries
    def account_id(self, delivery: Delivery) -> str | None: ...  # mapped to a tenant via installations
    def event_name(self, delivery: Delivery) -> str: ...
    def occurred_at(self, delivery: Delivery) -> datetime | None: ...  # the source's time; None = received_at
