"""Write episodes into the Postgres graph (EMBER-164): graph.nodes, graph.edges and graph.mentions, one graph
per connected scope (ontology.graph_scope). The counterpart to neo4j_writer.py, chosen with
GRAPH_STORE=postgres. It runs on load's own connection, inside load's transaction, so the graph and
episodes.loaded_at commit together (docs/research/postgres-graph.md, Part 1).

The rules, from Part 1 "Idempotency, versions and deletes":

- Newest wins: a node takes an episode's props only if the episode is not older than the node; an older
  one only fills what the node lacks, and never touches a deleted node.
- Tombstones: a delete not older than the node clears its content and ends every current edge of it,
  with invalidated_by = the delete's episode. The row stays, so a late edit can't bring it back.
- Placeholders: an edge to a node that hasn't loaded yet creates it bare (placeholder = true).
- Edges that disappear: a newer version of a record (OWNED) ends the current edges of the types it lists
  in full that it no longer lists. A late, older version's edge is stored already ended.
- Deterministic uuids, so applying an episode twice changes nothing.
- HAS_STATUS: a Jira issue's status is a fact on an edge to a Status node, from the webhook changelog.

Then the link step, for the nodes each episode touched: SAME_AS between a tenant's graphs (same source
identity, or same email across sources) and REFERENCES for Jira keys and pull request URLs in text. Link
edges have graph_id null and props.method, and never cross tenants.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from ontology import EDGE_TYPES, ENTITY_TYPES, ensure_graph, graph_scope

from .neo4j_writer import TOMBSTONE_KEYS
from .records import UUID_NAMESPACE, node_uuid

LABELS = set(ENTITY_TYPES) | {"Status"}
TYPES = set(EDGE_TYPES) | {"HAS_STATUS", "SAME_AS"}

# (source, kind) -> edge types the record lists in full around its main node (attrs nodes[0]), and which
# end of the edge that node is on.
OWNED = {
    ("jira", "issue"): {"ASSIGNED_TO": "dst", "RELATES_TO": "src", "HAS_STATUS": "src"},
    ("github", "issue"): {"ASSIGNED_TO": "dst"},
    ("github", "pull_request"): {"ASSIGNED_TO": "dst", "RESOLVES": "src"},
}

# The one-sentence fact per edge type, what edge search matches. {src} and {dst} are the nodes' names.
FACTS = {
    "AUTHORED": "{src} authored {dst}",
    "ASSIGNED_TO": "{src} is assigned to {dst}",
    "REVIEWED": "{src} reviewed {dst}",
    "MEMBER_OF": "{src} is a member of {dst}",
    "OWNS": "{src} owns {dst}",
    "RESOLVES_TO": "{src} is the account of {dst}",
    "CONTAINS": "{src} contains {dst}",
    "PART_OF": "{src} is part of {dst}",
    "REPLIES_TO": "{src} replies to {dst}",
    "REFERENCES": "{src} references {dst}",
    "RESOLVES": "{src} resolves {dst}",
    "RELATES_TO": "{src} relates to {dst}",
    "TOUCHES": "{src} touches {dst}",
    "DECIDED_IN": "{src} was decided in {dst}",
    "SUPERSEDES": "{src} supersedes {dst}",
    "AFFECTS": "{src} affects {dst}",
    "HAS_STATUS": "{src} has status {dst}",
    "SAME_AS": "{src} is the same person as {dst}",
}

PULL_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def _time(value) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) and value else None


def _status(episode) -> tuple[dict, dict] | None:
    """A Jira issue episode's (Status node, HAS_STATUS edge), from its record. Without a status change in
    the webhook changelog the fact is only known from this snapshot: props.valid_from = 'snapshot'."""
    if (episode.source, episode.kind) != ("jira", "issue") or episode.attrs.get("deleted"):
        return None
    payload = (episode.record or {}).get("payload") or {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else payload
    fields = issue.get("fields") or {}
    status = (fields.get("status") or {}).get("name")
    key = issue.get("key")
    if not status or not key:
        return None
    project = (fields.get("project") or {}).get("key") or key.rpartition("-")[0]
    items = (payload.get("changelog") or {}).get("items") or []
    moved = any(item.get("field") == "status" for item in items)
    status_key = f"{project}:{status}"
    return ({"label": "Status", "key": status_key, "props": {"name": status}},
            {"type": "HAS_STATUS", "src_label": "WorkItem", "src_key": key, "dst_label": "Status",
             "dst_key": status_key, "props": {} if moved else {"valid_from": "snapshot"}})


class Graph:
    """The graph one episode loads into."""

    def __init__(self, id_: int, group_id: str, tenant: str, source: str, scope: str = "") -> None:
        self.id, self.group_id, self.tenant, self.source, self.scope = id_, group_id, tenant, source, scope


class PgWriter:
    """GRAPH_STORE=postgres. LoadWorker hands write() its connection inside the batch's transaction."""

    async def write(self, conn, tenant: str, source: str, episodes) -> tuple[list[int], dict[int, Exception]]:
        """Apply episodes in order. Each row needs id, kind, occurred_at, attrs, and its source record's
        object_id and record. Returns (episode ids written, {episode id: why it failed}). Each episode runs
        in a savepoint, so a bad one fails alone; a transient error raises and rolls back the batch."""
        from .workers import is_transient

        graphs: dict[str, Graph] = {}
        scopes: dict[int, str] = {}
        failed: dict[int, Exception] = {}
        for episode in episodes:
            scope = graph_scope(source, episode.object_id or "", episode.record, episode.attrs)
            if scope is None:
                failed[episode.id] = ValueError(f"no graph scope in the {source} {episode.kind} record")
            else:
                scopes[episode.id] = scope
        # The per-graph lock, in a fixed order: the Graphiti fork takes the same one for its writes.
        new_graphs = []
        for scope in sorted(set(scopes.values())):
            existed = await conn.fetchval("select true from graph.graphs where (tenant, source, scope) = ($1, $2, $3)",
                                          tenant, source, scope)
            graph_id, group_id = await ensure_graph(conn, tenant, source, scope)
            await conn.execute("select pg_advisory_xact_lock(hashtext($1))", group_id)
            graphs[scope] = Graph(graph_id, group_id, tenant, source, scope)
            if not existed:
                new_graphs.append(graphs[scope])

        written = []
        for episode in episodes:
            if episode.id in failed:
                continue
            graph = graphs[scopes[episode.id]]
            try:
                async with conn.transaction():
                    await self._apply(conn, graph, episode)
                    await conn.execute("update episodes set graph_id = $1 where id = $2", graph.id, episode.id)
            except Exception as exc:
                if is_transient(exc):
                    raise
                failed[episode.id] = exc
                continue
            written.append(episode.id)
        first = {scopes[i]: i for i in reversed(written)}  # each graph's first episode written
        for graph in new_graphs:
            if graph.scope in first:
                await self._link_back(conn, graph, next(e for e in episodes if e.id == first[graph.scope]))
        return written, failed

    async def _apply(self, conn, graph: Graph, episode) -> None:
        attrs = episode.attrs
        nodes, edges = list(attrs["nodes"]), list(attrs["edges"])
        status = _status(episode)
        if status:
            nodes.append(status[0])
            edges.append(status[1])
        node_ids = [await self._node(conn, graph, episode, n) for n in nodes]
        touched = set(node_ids)
        for edge in edges:
            touched |= await self._edge(conn, graph, episode, edge)
        if nodes and not attrs.get("deleted"):
            await self._invalidate_missing(conn, graph, episode, nodes[0], edges)
        await conn.executemany("insert into graph.mentions values ($1, $2) on conflict do nothing",
                               [(node_id, episode.id) for node_id in sorted(touched)])
        await self._link(conn, graph.tenant, episode, node_ids)

    async def _ensure(self, conn, graph: Graph, label: str, key: str) -> int:
        """The node's id, created as a placeholder if its own episode hasn't loaded yet."""
        if label not in LABELS:
            raise ValueError(f"not a node label in the ontology: {label!r}")
        return await conn.fetchval(
            """with added as (
                 insert into graph.nodes (uuid, graph_id, tenant, label, key, placeholder) values ($1, $2, $3, $4, $5, true)
                 on conflict (graph_id, label, key) do nothing returning id)
               select id from added union all
               select id from graph.nodes where (graph_id, label, key) = ($2, $4, $5) limit 1""",
            node_uuid(graph.group_id, label, key), graph.id, graph.tenant, label, key)

    async def _node(self, conn, graph: Graph, episode, node: dict) -> int:
        node_id = await self._ensure(conn, graph, node["label"], node["key"])
        cur = await conn.fetchrow(
            "select occurred_at, deleted, props::text as props from graph.nodes where id = $1 for update", node_id)
        at = _time(node.get("occurred_at")) or episode.occurred_at
        newer = cur["occurred_at"] is None or at >= cur["occurred_at"]
        props = dict(node["props"])
        if props.pop("deleted", False):
            if newer:
                kept = {k: v for k, v in props.items() if k in TOMBSTONE_KEYS}
                await conn.execute(
                    """update graph.nodes set deleted = true, placeholder = false, name = null, summary = null,
                         props = $2::jsonb, embed_text = null, embedding = null, occurred_at = $3 where id = $1""",
                    node_id, json.dumps(kept), at)
                await conn.execute(
                    """update graph.edges set invalid_at = $2, invalidated_by = $3
                       where (src = $1 or dst = $1) and invalid_at is null and expired_at is null""",
                    node_id, at, episode.id)
            return node_id
        if cur["deleted"]:
            return node_id
        name, embed_text = props.pop("name", None), props.pop("embed_text", None)
        old = json.loads(cur["props"])
        merged = {**old, **props} if newer else {**props, **old}
        await conn.execute(
            """update graph.nodes set props = $2::jsonb, placeholder = false,
                 name = case when $5 then coalesce($3, name) else coalesce(name, $3) end,
                 embed_text = case when $5 then coalesce($4, embed_text) else coalesce(embed_text, $4) end,
                 occurred_at = case when $5 then $6 else occurred_at end
               where id = $1""",
            node_id, json.dumps(merged), name, embed_text, newer, at)
        return node_id

    async def _edge(self, conn, graph: Graph, episode, edge: dict) -> set[int]:
        if edge["type"] not in TYPES:
            raise ValueError(f"not a relationship type in the ontology: {edge['type']!r}")
        src = await self._ensure(conn, graph, edge["src_label"], edge["src_key"])
        dst = await self._ensure(conn, graph, edge["dst_label"], edge["dst_key"])
        at, invalid_at, invalidated_by = episode.occurred_at, None, None
        side = OWNED.get((episode.source, episode.kind), {}).get(edge["type"])
        if side:
            # A newer fact of this type already holds, so this one is history ended by it, as Graphiti's
            # _apply_temporal_invalidation does: a late arrival never becomes current.
            far = "dst" if side == "src" else "src"
            main, other = (src, dst) if side == "src" else (dst, src)
            later = await conn.fetchrow(
                f"""select valid_at, episode_id from graph.edges where {side} = $1 and type = $2 and {far} <> $3
                    and valid_at > $4 and invalid_at is null and expired_at is null order by valid_at limit 1""",
                main, edge["type"], other, at)
            if later:
                invalid_at, invalidated_by = later["valid_at"], later["episode_id"]
        # An endpoint already deleted: a late, older fact about it ended with the delete, a newer one is dropped
        gone = await conn.fetchrow(
            """select n.occurred_at, (select m.episode_id from graph.mentions m join episodes e on e.id = m.episode_id
                                      where m.node_id = n.id and e.attrs->>'deleted' = 'true'
                                      order by e.occurred_at desc limit 1) as episode_id
                 from graph.nodes n where n.id in ($1, $2) and n.deleted order by n.occurred_at limit 1""", src, dst)
        if gone:
            if at > gone["occurred_at"]:
                return {src, dst}
            if invalid_at is None or gone["occurred_at"] < invalid_at:
                invalid_at, invalidated_by = gone["occurred_at"], gone["episode_id"]
        key =(f"{node_uuid(graph.group_id, edge['src_label'], edge['src_key'])}|{edge['type']}|"
               f"{node_uuid(graph.group_id, edge['dst_label'], edge['dst_key'])}|{at.isoformat()}")
        await self._insert_edge(conn, key, graph.id, graph.tenant, edge["type"], src, dst, edge.get("props") or {},
                                at, invalid_at, episode.id, invalidated_by)
        return {src, dst}

    async def _insert_edge(self, conn, key: str, graph_id, tenant, type_, src, dst, props, valid_at, invalid_at,
                           episode_id, invalidated_by) -> None:
        """Insert unless this uuid exists or (src, type, dst) is already current, whatever its fact: a fact names
        its nodes, so a renamed node changes it. uuid5 of key, so a replay is a no-op. The conflict target is the
        pinned edges_current."""
        await conn.execute(
            """insert into graph.edges (uuid, graph_id, tenant, type, fact, src, dst, props, valid_at, invalid_at,
                                        episode_id, invalidated_by)
               select $1, $2, $3, $4, replace(replace($5, '{src}', coalesce(s.name, s.key)), '{dst}', coalesce(d.name, d.key)),
                      s.id, d.id, $8::jsonb, $9, $10, $11, $12
                 from graph.nodes s, graph.nodes d
                where s.id = $6 and d.id = $7
                  and not exists (select from graph.edges e where e.uuid = $1)
                  and not exists (select from graph.edges e where (e.src, e.type, e.dst) = ($6, $4, $7)
                                    and e.invalid_at is null and e.expired_at is null)
               on conflict (src, type, dst, md5(coalesce(fact, ''))) where invalid_at is null and expired_at is null
               do nothing""",
            uuid.uuid5(UUID_NAMESPACE, key), graph_id, tenant, type_, FACTS[type_], src, dst, json.dumps(props),
            valid_at, invalid_at, episode_id, invalidated_by)

    async def _invalidate_missing(self, conn, graph: Graph, episode, main: dict, edges: list[dict]) -> None:
        """Current edges of the OWNED types around the main node that this record no longer lists end at
        its time. Only edges that began by then: an older version never ends a newer fact."""
        for edge_type, side in OWNED.get((episode.source, episode.kind), {}).items():
            far = "dst" if side == "src" else "src"
            listed = [e[f"{far}_key"] for e in edges if e["type"] == edge_type and e[f"{side}_key"] == main["key"]]
            main_id = await self._ensure(conn, graph, main["label"], main["key"])
            await conn.execute(
                f"""update graph.edges e set invalid_at = $3, invalidated_by = $4
                    from graph.nodes other where other.id = e.{far} and e.{side} = $1 and e.type = $2
                    and e.invalid_at is null and e.expired_at is null and e.valid_at <= $3
                    and other.key <> all($5::text[])""",
                main_id, edge_type, episode.occurred_at, episode.id, listed)

    async def _link_back(self, conn, graph: Graph, episode) -> None:
        """A Jira or GitHub graph that didn't exist when the tenant's other text loaded: link that text now,
        once, so the links don't depend on which graph loaded first."""
        if graph.source == "jira":
            match, pattern = "~", r"\m" + graph.scope.rpartition("/")[2] + r"-\d+"
        elif graph.source == "github":
            match, pattern = "ilike", f"%github.com/{graph.scope}/pull/%"
        else:
            return
        ids = [r["id"] for r in await conn.fetch(
            f"""select id from graph.nodes where tenant = $1 and graph_id <> $2 and label in ('Message', 'Change')
                and not deleted and embed_text {match} $3""", graph.tenant, graph.id, pattern)]
        await self._link(conn, graph.tenant, episode, ids)

    async def _link(self, conn, tenant: str, episode, node_ids: list[int]) -> None:
        """Link edges from these nodes to nodes in the tenant's other graphs."""
        rows = await conn.fetch(
            """select n.id, n.uuid, n.label, n.props->>'email' as email, n.embed_text, n.key, n.graph_id, g.source
                 from graph.nodes n join graph.graphs g on g.id = n.graph_id
                where n.id = any($1) and not n.deleted and not n.placeholder""", node_ids)
        jira = None
        for row in rows:
            if row["label"] == "Person":
                # ponytail: scans the tenant's Person nodes; index props->>'email' if tenants grow large
                for other in await conn.fetch(
                        """select n.id, n.uuid, case when g.source = $3 then 'source_id' else 'email' end as method
                             from graph.nodes n join graph.graphs g on g.id = n.graph_id
                            where n.tenant = $1 and n.label = 'Person' and n.graph_id <> $2 and not n.deleted
                              and ((g.source = $3 and n.key = $4)
                                   or (g.source <> $3 and lower(n.props->>'email') = lower($5)))""",
                        tenant, row["graph_id"], row["source"], row["key"], row["email"]):
                    a, b = sorted([(str(row["uuid"]), row["id"]), (str(other["uuid"]), other["id"])])
                    await self._insert_edge(conn, f"{a[0]}|SAME_AS|{b[0]}", None, tenant, "SAME_AS", a[1], b[1],
                                            {"method": other["method"]}, episode.occurred_at, None, episode.id, None)
            if row["label"] not in ("Message", "Change") or not row["embed_text"]:
                continue
            targets: list[tuple[int, str, str, str, str]] = []  # (graph id, group_id, label, key, method)
            for repo, number in sorted(set(PULL_URL.findall(row["embed_text"]))):
                found = await conn.fetchrow(
                    "select id, group_id from graph.graphs where tenant = $1 and source = 'github' and scope = $2",
                    tenant, repo.lower())
                if found:
                    targets.append((found["id"], found["group_id"], "Change", f"{repo}#{number}", "pull_url"))
            if jira is None:  # the tenant's Jira projects: keys only match these, so UTF-8 is not a ticket
                jira = await conn.fetch("select id, group_id, split_part(scope, '/', 2) as project from graph.graphs"
                                        " where tenant = $1 and source = 'jira'", tenant)
            if jira:
                pattern = re.compile(r"\b(%s)-\d+\b" % "|".join(sorted({re.escape(g["project"]) for g in jira})))
                for match in sorted({m.group(0) for m in pattern.finditer(row["embed_text"])}):
                    targets += [(g["id"], g["group_id"], "WorkItem", match, "jira_key")
                                for g in jira if g["project"] == match.rpartition("-")[0]]
            for graph_id, group_id, label, key, method in targets:
                if graph_id == row["graph_id"]:
                    continue  # within one graph the mappers already link it
                dst = await self._ensure(conn, Graph(graph_id, group_id, tenant, ""), label, key)
                await self._insert_edge(conn, f"{row['uuid']}|REFERENCES|{node_uuid(group_id, label, key)}|link",
                                        None, tenant, "REFERENCES", row["id"], dst, {"method": method},
                                        episode.occurred_at, None, episode.id, None)
