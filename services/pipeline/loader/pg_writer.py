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
- HAS_STATUS and ASSIGNED_TO from a Jira issue's changelog: one fact per value held, from reconcile's
  issue.changelog.histories or a webhook's changelog.items, each ended when the next began.

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
SLACK_MARKUP = re.compile(r"<([^<>|]*)(?:\|([^<>]*))?>")  # <url|label>, <url>, <@U123>, <#C123|name>, <!here>
MENTION = re.compile(r"<@([A-Z0-9]+)")
NAME_LIMIT = 120


def _display(n: str) -> str:
    """SQL for what a fact calls node n: its name, else its id (a Person's account, a Conversation's channel)."""
    return (f"coalesce({n}.name, case {n}.label when 'Person' then substr({n}.key, strpos({n}.key, ':') + 1)"
            f" when 'Conversation' then split_part({n}.key, ':', 1) else {n}.key end)")


def _fact_sql(template: str) -> str:
    """SQL for a fact: the template with its nodes' display names filled in (aliases s and d)."""
    return f"replace(replace({template}, '{{src}}', {_display('s')}), '{{dst}}', {_display('d')})"


def _shorten(text: str, limit: int = NAME_LIMIT) -> str:
    """One line, cut at the last word boundary before limit with an ellipsis. A word longer than the
    limit (a URL) is kept whole, so a link is never cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = text.find(" ", limit)
        if cut < 0:
            return text
    return text[:cut].rstrip(" ,.;:") + "…"


def _slack_text(text: str, people: dict[str, str]) -> str:
    """Slack mrkdwn as a person reads it: <url|label> is label, <url> is url, <@U123> is the person's name
    (the id when unknown), <#C123|name> is #name."""
    def plain(match) -> str:
        target, label = match.group(1), match.group(2)
        if target.startswith("@"):
            return people.get(target[1:]) or label or target
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            return "@" + (label or target[1:])
        return label or target
    return SLACK_MARKUP.sub(plain, text)


def _person_name(user) -> str | None:
    if not isinstance(user, dict):
        return None
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    return (user.get("real_name") or profile.get("real_name") or profile.get("display_name")
            or user.get("display_name") or user.get("name") or None)


def _slack_names(record) -> tuple[dict[str, str], dict[str, str]]:
    """({user id: name}, {channel id: name}) that a Slack record carries: enrich's users.info and
    conversations.info copies, a user or channel record itself, or a message's user_profile."""
    record = record if isinstance(record, dict) else {}
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    message = record.get("message") if isinstance(record.get("message"), dict) else payload
    people, channels = {}, {}
    for user in (record.get("user"), record):
        if isinstance(user, dict) and str(user.get("id", "")).startswith(("U", "W", "B")) and _person_name(user):
            people[user["id"]] = _person_name(user)
    if message.get("user") and _person_name(message.get("user_profile")):
        people.setdefault(message["user"], _person_name(message["user_profile"]))
    for channel in (record.get("channel"), record):
        if isinstance(channel, dict) and str(channel.get("id", "")).startswith(("C", "G")) and channel.get("name"):
            channels[channel["id"]] = channel["name"]
    return people, channels


def _readable(node: dict, slack: bool, people: dict[str, str], channels: dict[str, str]) -> dict:
    """The node with a name an agent can read: a message's text without Slack markup, a Slack person or
    channel named from what the record carries, and every name cut to about NAME_LIMIT characters."""
    props = dict(node["props"])
    label = node["label"]
    if slack and label in ("Person", "Identity") and not props.get("name"):
        props["name"] = people.get(node["key"].removeprefix("slack:"))
    if slack and label == "Container" and not props.get("name"):
        props["name"] = channels.get(node["key"])
    if label == "Message" and props.get("embed_text"):
        props["name"] = _slack_text(props["embed_text"], people) if slack else props["embed_text"]
    if props.get("name"):
        props["name"] = _shorten(props["name"])
    return {**node, "props": {k: v for k, v in props.items() if v is not None}}




def _time(value) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) and value else None


def _jira_time(value) -> datetime | None:
    """A Jira time (2026-09-25T10:00:00.000+0000) or an ISO one."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (TypeError, ValueError):
        return _time(value)


def _held(entries, field: str, key: str, now, full: bool, created) -> list[tuple]:
    """(from when, value, its display name) per value a Jira field held, oldest first, from changelog entries
    [(time, entry)]. With the full changelog the first value is held since the issue was created; key is
    'String' for the name (status) or '' for the id (assignee). As the prototype's status_history."""
    moves = [(at, item) for at, entry in entries for item in entry.get("items") or [] if item.get("field") == field]
    held = []
    if full:
        first = moves[0][1] if moves else None
        held.append((created, first.get("from" + key) if first else now[0], first.get("fromString") if first
                     else now[1]))
    return held + [(at, item.get("to" + key), item.get("toString")) for at, item in moves]


def _jira_facts(episode) -> tuple[list[dict], list[dict]]:
    """A Jira issue episode's status (HAS_STATUS) and assignee (ASSIGNED_TO) facts, one per value held, each
    ended when the next began. They come from reconcile's issue.changelog.histories (expand=changelog) or a
    webhook's changelog.items (one change, at the episode's time). A status with no change behind it is only
    known from this snapshot: props.valid_from = 'snapshot'."""
    if (episode.source, episode.kind) != ("jira", "issue") or episode.attrs.get("deleted"):
        return [], []
    payload = (episode.record or {}).get("payload") or {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else payload
    fields, key = issue.get("fields") or {}, issue.get("key")
    status = (fields.get("status") or {}).get("name")
    if not key:
        return [], []
    project = (fields.get("project") or {}).get("key") or key.rpartition("-")[0]
    log = issue.get("changelog") or {}
    histories = log.get("histories") or []
    full = bool(histories) and (log.get("total") or 0) <= len(histories)  # expand=changelog stops at 100
    entries = sorted(((_jira_time(h.get("created")), h) for h in histories if _jira_time(h.get("created"))),
                     key=lambda pair: (pair[0], int(pair[1].get("id") or 0)))
    items = (payload.get("changelog") or {}).get("items") or []
    if not entries and items:
        entries = [(episode.occurred_at, {"items": items})]
    created = _jira_time(fields.get("created"))
    assignee = fields.get("assignee") or {}
    nodes, edges = [], []

    def facts(held, node, edge) -> None:
        for i, (start, value, name) in enumerate(held):
            if start is None or value is None:
                continue
            nodes.append(node(value, name, start))
            fact = {**edge(value), "valid_at": start}
            if i + 1 < len(held):
                fact["invalid_at"] = held[i + 1][0]
            edges.append(fact)

    statuses = _held(entries, "status", "String", (status, status), full, created)
    if not statuses and status:
        statuses = [(episode.occurred_at, status, status)]
    facts(statuses, lambda v, _n, _t: {"label": "Status", "key": f"{project}:{v}", "props": {"name": v}},
          lambda v: {"type": "HAS_STATUS", "src_label": "WorkItem", "src_key": key, "dst_label": "Status",
                     "dst_key": f"{project}:{v}", "props": {"valid_from": "snapshot"} if not entries else {}})
    facts(_held(entries, "assignee", "", (assignee.get("accountId"), assignee.get("displayName")), full, created),
          lambda v, n, t: {"label": "Person", "key": f"jira:{v}", "props": {"name": n}, "occurred_at": t.isoformat()},
          lambda v: {"type": "ASSIGNED_TO", "src_label": "Person", "src_key": f"jira:{v}", "dst_label": "WorkItem",
                     "dst_key": key, "props": {}})
    return nodes, edges


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
        history_nodes, history = _jira_facts(episode)
        # History first: its current fact starts when the change happened, and the mapper's copy of that
        # same fact (stamped with the episode's time) then finds it current and adds nothing.
        nodes, edges = list(attrs["nodes"]) + history_nodes, history + list(attrs["edges"])
        slack = graph.source == "slack"
        people, channels = _slack_names(episode.record) if slack else ({}, {})
        if slack:
            people.update(await self._people(conn, graph, [n["props"].get("embed_text") for n in nodes], people))
        nodes = [_readable(n, slack, people, channels) for n in nodes]
        renamed: set[int] = set()
        node_ids = [await self._node(conn, graph, episode, n, renamed) for n in nodes]
        if slack:
            renamed |= await self._rename_slack(conn, graph, node_ids, renamed)
        touched = set(node_ids)
        for edge in edges:
            touched |= await self._edge(conn, graph, episode, edge)
        if renamed:  # facts Ember wrote earlier (not Graphiti's: extractor_version 0) name the old names
            await conn.execute(
                f"""with hit as (select id from graph.edges where src = any($1) and expired_at is null
                             union select id from graph.edges where dst = any($1) and expired_at is null)
                    update graph.edges e set fact = {_fact_sql('t.template')}
                    from hit, graph.nodes s, graph.nodes d, unnest($2::text[], $3::text[]) t(type, template), episodes ep
                    where e.id = hit.id and s.id = e.src and d.id = e.dst and t.type = e.type
                      and ep.id = e.episode_id and ep.extractor_version > 0
                      and e.fact is distinct from {_fact_sql('t.template')}""",
                sorted(renamed), list(FACTS), list(FACTS.values()))
        if nodes and not attrs.get("deleted"):
            await self._invalidate_missing(conn, graph, episode, nodes[0], edges)
        await conn.executemany("insert into graph.mentions values ($1, $2) on conflict do nothing",
                               [(node_id, episode.id) for node_id in sorted(touched)])
        await self._link(conn, graph.tenant, episode, node_ids)

    async def _people(self, conn, graph: Graph, texts, known: dict[str, str]) -> dict[str, str]:
        """Names of the Slack users mentioned in texts that an earlier episode named."""
        ids = {m for text in texts if text for m in MENTION.findall(text)} - set(known)
        rows = await conn.fetch("select key, name from graph.nodes where graph_id = $1 and label = 'Person'"
                                " and key = any($2) and name is not null", graph.id, [f"slack:{i}" for i in ids])
        return {r["key"].removeprefix("slack:"): r["name"] for r in rows}

    async def _rename_slack(self, conn, graph: Graph, node_ids: list[int], renamed: set[int]) -> set[int]:
        """Names that depend on other nodes, in either load order: a thread is #channel once its channel is
        named, and a message mentioning a person who just got a name is renamed. Returns the nodes renamed."""
        changed = {r["id"] for r in await conn.fetch(
            """update graph.nodes c set name = '#' || ch.name from graph.nodes ch
                where c.graph_id = $2 and c.label = 'Conversation' and ch.graph_id = c.graph_id
                  and ch.label = 'Container' and ch.key = split_part(c.key, ':', 1) and ch.name is not null
                  and (c.id = any($1) or ch.id = any($1)) and c.name is distinct from '#' || ch.name
                returning c.id""", node_ids, graph.id)}
        named = [r["key"].removeprefix("slack:") for r in await conn.fetch(
            "select key from graph.nodes where id = any($1) and label = 'Person'", sorted(renamed))]
        if named:
            messages = await conn.fetch(
                """select id, name, embed_text from graph.nodes where graph_id = $1 and label = 'Message'
                    and embed_text like any($2) and not deleted""", graph.id, [f"%<@{i}%" for i in named])
            people = await self._people(conn, graph, [m["embed_text"] for m in messages], {})
            for message in messages:
                name = _shorten(_slack_text(message["embed_text"], people))
                if name != message["name"]:
                    await conn.execute("update graph.nodes set name = $2 where id = $1", message["id"], name)
                    changed.add(message["id"])
        return changed

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

    async def _node(self, conn, graph: Graph, episode, node: dict, renamed: set[int]) -> int:
        """Apply one node by the ordering rules; adds it to renamed when its name changed."""
        node_id = await self._ensure(conn, graph, node["label"], node["key"])
        cur = await conn.fetchrow(
            "select occurred_at, deleted, name, props::text as props from graph.nodes where id = $1 for update",
            node_id)
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
        stored = await conn.fetchval(
            """update graph.nodes set props = $2::jsonb, placeholder = false,
                 name = case when $5 then coalesce($3, name) else coalesce(name, $3) end,
                 embed_text = case when $5 then coalesce($4, embed_text) else coalesce(embed_text, $4) end,
                 occurred_at = case when $5 then $6 else occurred_at end
               where id = $1 returning name""",
            node_id, json.dumps(merged), name, embed_text, newer, at)
        if stored != cur["name"]:
            renamed.add(node_id)
        return node_id

    async def _edge(self, conn, graph: Graph, episode, edge: dict) -> set[int]:
        if edge["type"] not in TYPES:
            raise ValueError(f"not a relationship type in the ontology: {edge['type']!r}")
        src = await self._ensure(conn, graph, edge["src_label"], edge["src_key"])
        dst = await self._ensure(conn, graph, edge["dst_label"], edge["dst_key"])
        at, invalid_at, invalidated_by = edge.get("valid_at") or episode.occurred_at, None, None
        side = OWNED.get((episode.source, episode.kind), {}).get(edge["type"])
        if edge.get("invalid_at"):  # history: the changelog says when it ended
            invalid_at, invalidated_by = edge["invalid_at"], episode.id
        elif side:
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
        """Insert unless this uuid exists or, for a current fact, (src, type, dst) is already current whatever
        its fact: a fact names its nodes, so a renamed node changes it. uuid5 of key, so a replay is a no-op. The
        conflict target is the pinned edges_current."""
        await conn.execute(
            f"""insert into graph.edges (uuid, graph_id, tenant, type, fact, src, dst, props, valid_at, invalid_at,
                                         episode_id, invalidated_by)
               select $1, $2, $3, $4, {_fact_sql('$5')},
                      s.id, d.id, $8::jsonb, $9, $10, $11, $12
                 from graph.nodes s, graph.nodes d
                where s.id = $6 and d.id = $7
                  and not exists (select from graph.edges e where e.uuid = $1)
                  and ($10::timestamptz is not null or not exists (
                    select from graph.edges e where (e.src, e.type, e.dst) = ($6, $4, $7)
                      and e.invalid_at is null and e.expired_at is null))
               on conflict (src, type, dst, md5(coalesce(fact, ''))) where invalid_at is null and expired_at is null
               do nothing""",
            uuid.uuid5(UUID_NAMESPACE, key), graph_id, tenant, type_, FACTS[type_], src, dst, json.dumps(props),
            valid_at, invalid_at, episode_id, invalidated_by)

    async def _invalidate_missing(self, conn, graph: Graph, episode, main: dict, edges: list[dict]) -> None:
        """Current edges of the OWNED types around the main node that this record no longer lists end at
        its time. Only edges that began by then: an older version never ends a newer fact."""
        for edge_type, side in OWNED.get((episode.source, episode.kind), {}).items():
            far = "dst" if side == "src" else "src"
            listed = [e[f"{far}_key"] for e in edges if e["type"] == edge_type and e[f"{side}_key"] == main["key"]
                      and not e.get("invalid_at")]
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
