"""Check mapped nodes and edges against the ontology before anything is written."""

from __future__ import annotations

from pydantic import ValidationError

from ontology import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES
from ontology.entities import SourceRef

from .mappers.base import Mapped, Reject
from .records import EdgeRec, NodeRec


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    return f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"


_PRIMITIVES = (str, int, float, bool, type(None))


def _check_primitive_props(what: str, props: dict) -> None:
    """Neo4j stores only primitives and lists of primitives as properties; a map (or a list
    holding one) aborts the whole write with a CypherTypeError, so reject the record instead."""
    for key, value in props.items():
        if isinstance(value, _PRIMITIVES):
            continue
        if isinstance(value, list) and all(isinstance(item, _PRIMITIVES) for item in value):
            continue
        raise Reject(f"{what}: property {key!r} is a {type(value).__name__} (or holds a non-primitive); "
                     "Neo4j properties must be str, int, float, bool or lists of those")


def validate_node(node: NodeRec, source: str) -> None:
    model = ENTITY_TYPES.get(node.label)
    if model is None:
        raise Reject(f"unknown node label {node.label!r}")
    _check_primitive_props(f"{node.label} {node.key!r}", node.props)
    attrs = {k: v for k, v in node.props.items() if k in model.model_fields}
    try:
        model.model_validate(attrs)
    except ValidationError as exc:
        raise Reject(f"{node.label} {node.key!r}: {_first_error(exc)}") from exc
    if issubclass(model, SourceRef):
        if node.props.get("source") != source:
            raise Reject(f"{node.label} {node.key!r}: source {node.props.get('source')!r} does not match {source!r}")
        if node.props.get("external_id") != node.key:
            raise Reject(f"{node.label} {node.key!r}: external_id does not match its key")


def validate_edge(edge: EdgeRec) -> None:
    model = EDGE_TYPES.get(edge.type)
    if model is None:
        raise Reject(f"unknown relationship type {edge.type!r}")
    if edge.type not in EDGE_TYPE_MAP.get((edge.src_label, edge.dst_label), []):
        raise Reject(f"{edge.type} not allowed from {edge.src_label} to {edge.dst_label}")
    _check_primitive_props(edge.type, edge.props)
    attrs = {k: v for k, v in edge.props.items() if k in model.model_fields}
    try:
        model.model_validate(attrs)
    except ValidationError as exc:
        raise Reject(f"{edge.type}: {_first_error(exc)}") from exc


def validate_mapped(mapped: Mapped, source: str) -> None:
    for node in mapped.nodes:
        validate_node(node, source)
    for edge in mapped.edges:
        validate_edge(edge)
