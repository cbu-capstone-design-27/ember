"""Graph records the mappers produce and the writer consumes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

_NAMESPACE = uuid.UUID("6f1c2b4e-0a1d-4c53-9d57-3f4f0f8c2a10")


def node_uuid(group_id: str, label: str, key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{group_id}|{label}|{key}"))


def _drop_none(props: dict[str, Any]) -> dict[str, Any]:
    # A None would erase a property a richer record already set when merged.
    return {k: v for k, v in props.items() if v is not None}


@dataclass
class NodeRec:
    label: str
    group_id: str
    key: str
    props: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.props = _drop_none(self.props)

    @property
    def uuid(self) -> str:
        return node_uuid(self.group_id, self.label, self.key)


@dataclass
class EdgeRec:
    type: str
    group_id: str
    src_label: str
    src_key: str
    dst_label: str
    dst_key: str
    props: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.props = _drop_none(self.props)

    @property
    def src_uuid(self) -> str:
        return node_uuid(self.group_id, self.src_label, self.src_key)

    @property
    def dst_uuid(self) -> str:
        return node_uuid(self.group_id, self.dst_label, self.dst_key)
