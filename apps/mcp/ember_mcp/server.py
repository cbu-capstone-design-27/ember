"""Ember's MCP server on stdio (EMBER-79): search and read the history Ember ingested into source_records."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import retrieval

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

mcp = MCPServer(
    "ember",
    instructions="Search the team's GitHub, Jira, Slack and GitLab history that Ember has ingested.",
)


@mcp.tool(annotations=READ_ONLY)
async def search(question: str, k: int = 8, sources: list[str] | None = None) -> list[retrieval.Hit]:
    """Search Ember's GitHub, Jira, Slack and GitLab history for issues, pull requests, merge requests,
    comments, commits and chat messages that match a question. Use it to find why something was built,
    who discussed it, or which ticket, PR or thread covers a topic.

    Matching is by meaning (vector search), so ask a natural-language question; hits don't need to
    share its words. Only records the embed worker has processed are searchable, so very recent
    activity may be missing.

    Args:
        question: What you want to know, as a question or description.
        k: How many hits to return, best first (1 to 50).
        sources: Only search these sources, from "github", "jira", "slack", "gitlab". Omit for all.

    Each hit has source, kind, object_id, version, occurred_at, title, snippet (the start of the
    text that was embedded), url (link to the original, when known) and score (cosine similarity,
    higher is closer). Pass a hit's source and object_id to get_record to read it in full.
    """
    if not 1 <= k <= 50:
        raise ToolError("k must be between 1 and 50")
    try:
        return await retrieval.search(question, k, sources)
    except (retrieval.DatabaseError, retrieval.EmbeddingError) as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=READ_ONLY)
async def get_record(source: str, object_id: str) -> dict[str, Any]:
    """Read one search hit in full: the newest stored version of the object, with its whole source
    payload as `record` (the webhook or API object plus anything Ember fetched). Records over 50,000
    characters come back as truncated JSON text with `truncated: true`.

    Args:
        source: The hit's source, e.g. "jira".
        object_id: The hit's object_id, e.g. "10004" or "org/repo#42".
    """
    try:
        found = await retrieval.get_record(source, object_id)
    except retrieval.DatabaseError as e:
        raise ToolError(str(e)) from e
    if found is None:
        raise ToolError(f"No {source} record with object_id {object_id!r}")
    return found
