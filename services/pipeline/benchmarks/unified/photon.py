"""Replay the committed photon source rows through the existing extractor, entirely in memory."""

from datetime import datetime

from graphiti_test.data import load_rows
from loader.extract import SourceRow, extract, graph_records
from loader.memory_writer import MemoryWriter
from loader.records import ordering_time

from .graph import from_memory


async def export_photon(as_of: datetime | None = None):
    """Rebuild as of source time, rather than filtering today's nodes (which leaks future versions)."""
    if as_of and as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    rows = [row for row in load_rows() if as_of is None or row.occurred_at <= as_of]
    keys = {r.object_id: r.record["issue"]["key"] for r in rows if r.source == "jira" and r.kind == "issue"}

    async def lookup(tenant, issue_id):
        return keys.get(issue_id)

    writer = MemoryWriter()
    for i, row in enumerate(rows, 1):
        record = {"payload": row.record} if row.source == "slack" else row.record
        source = SourceRow(i, row.source, "photon", row.kind, row.object_id, row.version, row.occurred_at, record)
        draft = await extract(source, lookup)
        nodes, edges = graph_records(draft.attrs, ordering_time(row.occurred_at))
        writer.write_nodes(nodes)
        writer.write_edges(edges, placeholders=True)
    return from_memory(writer, "photon")
