"""Loading-correctness suite: does the graph match what the source data says it should contain?"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass
class Check:
    group: str  # a source name, or "all" for checks about the whole run
    name: str
    ok: bool
    detail: str = ""


def run_loading(spec: dict, report, snapshot) -> list[Check]:
    checks: list[Check] = []
    skipped = sum(report.skipped.values())
    total = report.mapped + report.rejected + skipped
    checks.append(Check("all", "records accounted for", report.records == total,
                        f"{report.records} read, {report.mapped} mapped + {skipped} skipped + {report.rejected} rejected"))
    rate = report.rejected / report.records if report.records else 0.0
    limit = spec.get("max_reject_rate", 0.0)
    checks.append(Check("all", "reject rate", rate <= limit, f"{rate:.1%} rejected, limit {limit:.1%}"))
    checks.append(Check("all", "schema check", not report.problems, "; ".join(report.problems)))

    counts = Counter((n["source"], n["label"]) for n in snapshot.nodes)
    for source, labels in sorted(spec.get("nodes", {}).items()):
        for label, expected in sorted(labels.items()):
            found = counts.get((source, label), 0)
            checks.append(Check(source, f"nodes {label}", found == expected, f"expected {expected}, found {found}"))

    by_ref = {(n["label"], n["ref"]): n for n in snapshot.nodes}
    for item in spec.get("fields", []):
        source = item["ref"].split(":", 1)[0]
        node = by_ref.get((item["label"], item["ref"]))
        for prop, expected in sorted(item["props"].items()):
            name = f"field {item['ref']} {prop}"
            if node is None:
                checks.append(Check(source, name, False, f"no {item['label']} with that ref"))
            else:
                actual = node["props"].get(prop)
                checks.append(Check(source, name, actual == expected, f"expected {expected!r}, found {actual!r}"))

    edge_set = {(e["type"], e["src_label"], e["src"], e["dst_label"], e["dst"]) for e in snapshot.edges}
    for edge in spec.get("edges", []):
        key = (edge["type"], edge["src_label"], edge["src"], edge["dst_label"], edge["dst"])
        checks.append(Check(edge["src"].split(":", 1)[0], f"edge {edge['type']} {edge['src']} -> {edge['dst']}",
                            key in edge_set, "" if key in edge_set else "not in the graph"))
    return checks


def score_loading(checks: list[Check]) -> dict[str, dict]:
    groups: dict[str, list[Check]] = {}
    for check in checks:
        groups.setdefault(check.group, []).append(check)
    return {group: {"passed": sum(c.ok for c in items), "total": len(items),
                    "percent": round(100 * sum(c.ok for c in items) / len(items), 1)}
            for group, items in sorted(groups.items())}
