"""Ember's MCP server on stdio (EMBER-79, EMBER-166): search and read the history Ember ingested and the
graph built from it, for the user in EMBER_USER_EMAIL, limited to their tenants by row-level security."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import retrieval

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ERRORS = (retrieval.DatabaseError, retrieval.EmbeddingError, retrieval.AccessError)

mcp = MCPServer(
    "ember",
    instructions="Search the team's GitHub, Jira, Slack and GitLab history that Ember has ingested, and the "
                 "graph of facts built from it: who is assigned to what, which threads reference which tickets, "
                 "and how that changed over time.",
)


@mcp.tool(annotations=READ_ONLY)
async def search(question: str, k: int = 8, sources: list[str] | None = None,
                 graphs: list[str] | None = None) -> list[retrieval.Hit]:
    """Search Ember's GitHub, Jira, Slack and GitLab history for issues, pull requests, merge requests,
    comments, commits and chat messages that match a question. Use it to find why something was built,
    who discussed it, or which ticket, PR or thread covers a topic.

    It fuses search by meaning over the records with a search of the knowledge graph (its nodes, its
    facts and the record text), so ask a natural-language question. Very recent activity may be missing.

    Args:
        question: What you want to know, as a question or description.
        k: How many hits to return, best first (1 to 50).
        sources: Only search these sources, from "github", "jira", "slack", "gitlab". Omit for all.
        graphs: Only search these graphs, by name or scope (e.g. "org/repo", "site.atlassian.net/KEY",
            a Slack team id). whoami lists them. Omit for all.

    An identifier in the question (a Jira key like "PHO-87", "owner/repo#12", or "#12" when there is one
    GitHub repo) is looked up exactly: its record comes first (score 1), then the records that reference
    it, such as Slack messages and comments (score 0.5).

    Each hit has source, kind, object_id, version, occurred_at, title, snippet, url (link to the
    original, when known), score (fused rank score, higher is better) and related: up to five facts
    that hold now around the hit's graph node ("X is assigned to Y", "message references PHO-4").
    Pass a hit's source and object_id to get_record or history.
    """
    if not 1 <= k <= 50:
        raise ToolError("k must be between 1 and 50")
    try:
        return await retrieval.search(question, k, sources, graphs)
    except ERRORS as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=READ_ONLY)
async def get_record(source: str, object_id: str, raw: bool = False) -> dict[str, Any]:
    """Read one record: the newest stored version of the object, as title, key, url, kind, status,
    assignee, reporter, created, updated, labels, description (plain text) and comment_count, with its
    graph `node` (label, key, name, summary), the `facts` that hold around it now (assignees, status,
    links to threads, PRs and people), and its `graph` (name, scope).

    Args:
        source: The hit's source, e.g. "jira".
        object_id: The hit's object_id, e.g. "10004" or "org/repo#42". For Jira, the key ("PHO-87") works too.
        raw: Return the whole stored source payload as `record` instead of the compact fields. Records over
            50,000 characters come back as truncated JSON text with `truncated: true`.
    """
    try:
        found = await retrieval.get_record(source, object_id, raw)
    except ERRORS as e:
        raise ToolError(str(e)) from e
    if found is None:
        raise ToolError(f"No {source} record with object_id {object_id!r}")
    return found


@mcp.tool(annotations=READ_ONLY)
async def history(source: str, object_id: str) -> list[dict[str, Any]]:
    """How one object changed: every fact about its graph node, ended ones included, oldest first. Each
    has type, fact, valid_at (when it began), invalid_at (when it ended, null if it still holds), set_by
    and ended_by: the episode and source record version (source, object_id, version) that set or ended
    it. Read that version with get_record's source and object_id to see why. Use it for "who was
    assigned before", "when did the status change" and "what changed it".

    Args:
        source: The hit's source, e.g. "jira".
        object_id: The hit's object_id, e.g. "10004". For Jira, the key ("PHO-87") works too.
    """
    try:
        found = await retrieval.history(source, object_id)
    except ERRORS as e:
        raise ToolError(str(e)) from e
    if found is None:
        raise ToolError(f"No {source} object {object_id!r} in the graph")
    return found


@mcp.tool(annotations=READ_ONLY)
async def whoami() -> dict[str, Any]:
    """Who this server reads as: the email, the tenants (organizations and personal workspace) it can
    read, every graph it sees (tenant, source, scope, name, node count) and how search runs."""
    try:
        return await retrieval.whoami()
    except ERRORS as e:
        raise ToolError(str(e)) from e
