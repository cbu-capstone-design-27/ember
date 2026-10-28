"""In-memory writer for unit tests and --dry-run. Same interface as Neo4jWriter."""

from __future__ import annotations

from ontology import parse_subgraph_id

from .facts import SOURCE_BACKED, GraphFacts, expected_constraint_names
from .records import EdgeRec, NodeRec
from .snapshot import HIDDEN_PROPS, Snapshot, make_ref


def _apply(stored: dict, props: dict, occurred_at: str | None, deletable: bool) -> None:
    """Neo4jWriter's ordering rule (neo4j_writer._guarded_set): a row not older than the stored
    occurred_at sets its props; an older one only fills what is missing, and nothing on a deleted node."""
    current = stored.get("occurred_at")
    if occurred_at is None or current is None or occurred_at >= current:
        stored.update(props)
        if occurred_at is not None:
            stored["occurred_at"] = occurred_at
    elif not (deletable and stored.get("deleted")):
        for key, value in props.items():
            stored.setdefault(key, value)


class MemoryWriter:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.identity = None

    def read_embedding_identity(self):
        return self.identity

    def write_embedding_identity(self, identity) -> None:
        if self.identity is None:
            self.identity = identity

    def embed_candidates(self, group_ids: list[str]) -> list[dict]:
        wanted = set(group_ids)
        return [
            {"label": label, "uuid": uuid_, "group_id": props["group_id"], "key": props.get("external_id"),
             "text": props["embed_text"], "hash": props.get("embedding_text_hash"),
             "vector": props.get("embedding")}
            for (label, uuid_), props in self.nodes.items()
            if props["group_id"] in wanted and (props.get("embed_text") or "").strip()
        ]

    def write_embeddings(self, rows: list[dict]) -> None:
        for row in rows:
            node = self.nodes[(row["label"], row["uuid"])]
            node["embedding"], node["embedding_text_hash"] = row["vector"], row["hash"]

    def ensure_schema(self) -> None:
        pass

    def reset(self, group_ids: list[str]) -> None:
        wanted = set(group_ids)
        self.nodes = {k: v for k, v in self.nodes.items() if v["group_id"] not in wanted}
        self.edges = {k: v for k, v in self.edges.items() if v["group_id"] not in wanted}

    def write_nodes(self, nodes: list[NodeRec]) -> int:
        for n in nodes:
            stored = self.nodes.setdefault((n.label, n.uuid), {})
            _apply(stored, n.props, n.occurred_at, deletable=True)
            stored.pop("placeholder", None)
            stored.update({"uuid": n.uuid, "group_id": n.group_id})
        return len(nodes)

    def write_edges(self, edges: list[EdgeRec], placeholders: bool = False) -> int:
        written = 0
        for e in edges:
            if placeholders:
                source = parse_subgraph_id(e.group_id)[1]
                for label, uuid_, key in ((e.src_label, e.src_uuid, e.src_key), (e.dst_label, e.dst_uuid, e.dst_key)):
                    if label in SOURCE_BACKED and (label, uuid_) not in self.nodes:
                        self.nodes[(label, uuid_)] = {"group_id": e.group_id, "uuid": uuid_, "source": source,
                                                      "external_id": key, "placeholder": True}
            if (e.src_label, e.src_uuid) in self.nodes and (e.dst_label, e.dst_uuid) in self.nodes:
                key = (e.type, e.src_label, e.src_uuid, e.dst_label, e.dst_uuid)
                stored = self.edges.setdefault(key, {})
                _apply(stored, e.props, e.occurred_at, deletable=False)
                stored["group_id"] = e.group_id
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

    def snapshot(self, group_ids: list[str]) -> Snapshot:
        wanted = set(group_ids)
        snap, refs = Snapshot(), {}
        for (label, uuid_), props in self.nodes.items():
            if props["group_id"] in wanted and props.get("source") and props.get("external_id"):
                refs[(label, uuid_)] = make_ref(props["source"], props["external_id"])
                snap.nodes.append({"label": label, "ref": refs[(label, uuid_)], "source": props["source"],
                                   "props": {k: v for k, v in props.items() if k not in HIDDEN_PROPS}})
        for (rel, src_label, src_uuid, dst_label, dst_uuid), props in self.edges.items():
            src, dst = refs.get((src_label, src_uuid)), refs.get((dst_label, dst_uuid))
            if src and dst and props["group_id"] in wanted:
                snap.edges.append({"type": rel, "src_label": src_label, "src": src,
                                   "dst_label": dst_label, "dst": dst})
        return snap
