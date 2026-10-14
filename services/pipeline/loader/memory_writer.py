"""In-memory writer for unit tests and --dry-run. Same interface as Neo4jWriter."""

from __future__ import annotations

from .facts import SOURCE_BACKED, GraphFacts, expected_constraint_names
from .records import EdgeRec, NodeRec


class MemoryWriter:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: dict[tuple, dict] = {}

    def ensure_schema(self) -> None:
        pass

    def reset(self, group_ids: list[str]) -> None:
        wanted = set(group_ids)
        self.nodes = {k: v for k, v in self.nodes.items() if v["group_id"] not in wanted}
        self.edges = {k: v for k, v in self.edges.items() if v["group_id"] not in wanted}

    def write_nodes(self, nodes: list[NodeRec]) -> int:
        for n in nodes:
            stored = self.nodes.setdefault((n.label, n.uuid), {})
            stored.update(n.props)
            stored.update({"uuid": n.uuid, "group_id": n.group_id})
        return len(nodes)

    def write_edges(self, edges: list[EdgeRec]) -> int:
        written = 0
        for e in edges:
            if (e.src_label, e.src_uuid) in self.nodes and (e.dst_label, e.dst_uuid) in self.nodes:
                key = (e.type, e.src_label, e.src_uuid, e.dst_label, e.dst_uuid)
                self.edges.setdefault(key, {}).update({**e.props, "group_id": e.group_id})
                written += 1
        return written

    def read_facts(self, group_ids: list[str]) -> GraphFacts:
        wanted = set(group_ids)
        facts = GraphFacts(constraints=expected_constraint_names())  # stands in for SHOW CONSTRAINTS
        for (label, _), props in self.nodes.items():
            if props["group_id"] not in wanted:
                continue
            facts.labels.add(label)
            facts.node_counts[label] = facts.node_counts.get(label, 0) + 1
            if label in SOURCE_BACKED and not all(props.get(k) for k in ("source", "kind", "external_id")):
                facts.missing_required[label] = facts.missing_required.get(label, 0) + 1
        for (rel, src, _, dst, _), props in self.edges.items():
            if props["group_id"] in wanted:
                triple = (src, rel, dst)
                facts.relationships[triple] = facts.relationships.get(triple, 0) + 1
        return facts
