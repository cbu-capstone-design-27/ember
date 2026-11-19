from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Report:
    records: int = 0
    mapped: int = 0
    rejected: int = 0
    skipped: Counter = field(default_factory=Counter)
    nodes_written: int = 0
    edges_sent: int = 0
    edges_written: int = 0
    node_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rejects_path: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if self.rejected or self.problems else 0

    def render(self) -> str:
        skipped = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items()) if v) or "none"
        lines = [
            f"records read:   {self.records}",
            f"mapped:         {self.mapped}",
            f"skipped:        {skipped}",
            f"rejected:       {self.rejected}" + (f" (see {self.rejects_path})" if self.rejected else ""),
            f"nodes written:  {self.nodes_written}",
            # "written" counts merge hits: an edge whose endpoint is absent is dropped, not written.
            f"edges written:  {self.edges_written} merged of {self.edges_sent} sent "
            f"({self.edges_sent - self.edges_written} dropped, endpoint not in graph)",
        ]
        if self.node_counts:
            lines.append("nodes in graph: " + ", ".join(f"{k}={v}" for k, v in sorted(self.node_counts.items())))
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        if self.problems:
            lines.append(f"schema check: {len(self.problems)} mismatches")
            lines += [f"  - {p}" for p in self.problems]
        else:
            lines.append("schema check: OK")
        return "\n".join(lines)
