"""Scratch prototype for docs/research/postgres-graph.md (EMBER-118): load the photon data into graph.nodes,
graph.edges and graph.mentions, then answer "what was this ticket's status on a date, and why did it change".

Rows come from apps/mcp/seed_photon.py and episodes from services/pipeline's extract, unchanged. Two kinds of
record have no mapper yet, so gap_episodes() below maps them: Jira changelog entries, which hold the status
history, and remote links, which hold the pull requests. No Spark, no embeddings, no Graphiti.

Run against a scratch database that has the ingestion migrations and the vector extension. It deletes tenant
photon's rows and drops schema graph:

    DATABASE_URL=postgresql://postgres:pw@localhost:55441/ember?sslmode=disable \
        python docs/research/postgres-graph-prototype.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "services" / "pipeline"), str(REPO / "apps" / "mcp")]

import seed_photon  # noqa: E402
from loader.extract import EXTRACTOR_VERSION, SourceRow, extract  # noqa: E402
from loader.mappers import Reject, Skip  # noqa: E402

TENANT = "photon"
LOAD_BATCH = 100
NAMESPACE = uuid.UUID("0d7c5f6e-6a55-4a4b-9a55-4f6b6a1c1e18")
TICKET, BEFORE, AFTER = "PHO-104", "2026-09-25T12:00:00+00:00", "2026-09-29T12:00:00+00:00"
PULL_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")

# The tables as the doc specifies them, plus the episodes.uuid column it adds.
SCHEMA = """
drop schema if exists graph cascade;
create schema graph;
alter table episodes add column if not exists uuid uuid;

create table graph.nodes (
  id          bigint generated always as identity primary key,
  uuid        uuid not null unique,
  tenant      text not null,
  source      text not null,
  label       text not null,
  key         text not null,
  name        text,
  summary     text,
  props       jsonb not null default '{}',
  placeholder boolean not null default false,
  deleted     boolean not null default false,
  occurred_at timestamptz,
  embed_text  text,
  embedding   vector(1024),
  search      tsvector generated always as (to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(embed_text, ''))) stored,
  unique (tenant, source, label, key)
);
create index nodes_embedding on graph.nodes using hnsw (embedding vector_cosine_ops);
create index nodes_search on graph.nodes using gin (search);

create table graph.edges (
  id             bigint generated always as identity primary key,
  uuid           uuid not null unique,
  tenant         text not null,
  source         text not null,
  type           text not null,
  src            bigint not null references graph.nodes on delete cascade,
  dst            bigint not null references graph.nodes on delete cascade,
  fact           text,
  fact_embedding vector(1024),
  props          jsonb not null default '{}',
  valid_at       timestamptz not null,
  invalid_at     timestamptz,
  created_at     timestamptz not null default now(),
  expired_at     timestamptz,
  episode_id     bigint references episodes (id) on delete set null,
  invalidated_by bigint references episodes (id) on delete set null,
  search         tsvector generated always as (to_tsvector('simple', coalesce(fact, ''))) stored
);
create unique index edges_current on graph.edges (src, type, dst) where invalid_at is null and expired_at is null;
create index edges_out on graph.edges (src, type) include (dst, valid_at, invalid_at) where expired_at is null;
create index edges_in  on graph.edges (dst, type) include (src, valid_at, invalid_at) where expired_at is null;
create index edges_fact_embedding on graph.edges using hnsw (fact_embedding vector_cosine_ops);
create index edges_search on graph.edges using gin (search);

create table graph.mentions (
  node_id    bigint references graph.nodes on delete cascade,
  episode_id bigint references episodes (id) on delete cascade,
  primary key (node_id, episode_id)
);
"""

# Edge types a record lists in full around its WorkItem, and which end the WorkItem is on (doc, "Edges that
# disappear"). A newer version invalidates current edges of these types that it no longer lists.
OWNED = {("jira", "issue"): {"ASSIGNED_TO": "dst", "RELATES_TO": "src"}, ("jira", "changelog"): {"HAS_STATUS": "src"}}


def jira_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def fixture_bodies() -> list[dict]:
    return seed_photon.bodies("jira-intake.jsonl")


def status_history(issue: dict, page: dict | None) -> list[tuple[datetime, str, dict | None]]:
    """(from when, status, changelog entry) per status the issue held, oldest first, from the raw changelog."""
    entries = sorted(page["values"] if page else [], key=lambda h: (jira_time(h["created"]), int(h["id"])))
    moves = [(h, item) for h in entries for item in h["items"] if item["field"] == "status"]
    first = moves[0][1]["fromString"] if moves else issue["fields"]["status"]["name"]
    return [(jira_time(issue["fields"]["created"]), first, None)] + [
        (jira_time(h["created"]), item["toString"], h) for h, item in moves]


def fixture_issues() -> dict[str, tuple[dict, dict | None, list[dict]]]:
    """Issue key -> (issue, changelog page, remote links), from the fixture as the connector pulled it."""
    bodies = fixture_bodies()
    pages, links = {}, {}
    for body in bodies:
        match = re.search(r"/issue/([A-Z]+-\d+)/(changelog|remotelink)", body.get("self", ""))
        if match and match.group(2) == "changelog" and "values" in body:
            pages[match.group(1)] = body
        elif match and match.group(2) == "remotelink" and "globalId" in body:
            links.setdefault(match.group(1), []).append(body)
    return {b["key"]: (b, pages.get(b["key"]), links.get(b["key"], [])) for b in bodies if "fields" in b and "key" in b}


def gap_rows() -> list[tuple]:
    """source_records rows for what seed_photon leaves out: one per status held, one per remote link."""
    rows = []
    for key, (issue, page, links) in fixture_issues().items():
        project = issue["fields"]["project"]["key"]
        for since, status, entry in status_history(issue, page):
            version = entry["id"] if entry else "created"
            rows.append(("jira", "changelog", f"{key}/changelog/{version}", version, since,
                         {"key": key, "project": project, "status": status, "history": entry, "page": page and page["self"]}))
        for link in links:
            rows.append(("jira", "remote_link", f"{key}/remotelink/{link['id']}", issue["fields"]["updated"],
                         jira_time(issue["fields"]["updated"]), {"key": key, "link": link}))
    return rows


def gap_episode(kind: str, row: SourceRow) -> tuple[str, dict]:
    """(text, attrs) for the kinds extract has no mapper for. Attrs has extract's shape."""
    rec = row.record
    if kind == "changelog":
        history = rec["history"]
        who = history["author"]["displayName"] if history else None
        text = f"{rec['key']} status {rec['status']}" + (f", set by {who}" if who else ", when created")
        status_key = f"{rec['project']}:{rec['status']}"
        return text, {
            "nodes": [{"label": "Status", "key": status_key, "props": {"name": rec["status"]}}],
            "edges": [{"type": "HAS_STATUS", "src_label": "WorkItem", "src_key": rec["key"], "dst_label": "Status",
                       "dst_key": status_key, "props": {}, "fact": text}],
            "author": who, "items": history["items"] if history else [], "page": rec["page"],
        }
    obj = rec["link"]["object"]
    match = PULL_URL.search(obj["url"])
    if not match:  # the GitHub issue link: photon has no GitHub subgraph to join it to, so it stays on the record
        return f"{rec['key']} links {obj['url']}", {"nodes": [], "edges": []}
    change = f"{match.group(1)}#{match.group(2)}"
    return f"{rec['key']} links pull request {change}", {
        "nodes": [{"label": "Change", "key": change, "source": "github", "placeholder": True,
                   "props": {"name": obj["title"], "url": obj["url"]}}],
        "edges": [{"type": "REFERENCES", "src_label": "WorkItem", "src_key": rec["key"], "dst_label": "Change",
                   "dst_key": change, "dst_source": "github", "edge_source": "link", "props": {},
                   "fact": f"{rec['key']} references {change}"}],
    }


async def add_episode(db, key, source, kind, occurred_at, text, attrs, record_id=None) -> int:
    return await db.fetchval(
        """insert into episodes (key, uuid, tenant, source, kind, occurred_at, text, attrs, source_record_id, extractor_version)
           values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10) returning id""",
        key, uuid.uuid5(NAMESPACE, key), TENANT, source, kind, occurred_at, text, json.dumps(attrs), record_id,
        EXTRACTOR_VERSION)


async def seed_and_extract(db) -> tuple[int, int]:
    """source_records from seed_photon and gap_rows, episodes from extract and gap_episode."""
    await seed_photon.main(embed_rows=False)
    await db.executemany(
        "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
        " values ($1, 'photon', $2, $3, $4, $5, $6::jsonb) on conflict do nothing",
        [(*row[:5], json.dumps(row[5])) for row in gap_rows()])

    async def no_lookup(_tenant, _issue_id):  # seed_photon's comments carry their issue's key
        return None

    skipped = 0
    rows = await db.fetch("select * from source_records where tenant = $1 order by id", TENANT)
    for r in rows:
        row = SourceRow(r["id"], r["source"], r["tenant"], r["kind"], r["object_id"], r["version"], r["occurred_at"],
                        json.loads(r["record"]))
        key = f"{row.tenant}:{row.source}:{row.kind}:{row.object_id}@{row.version}"
        if row.kind in ("changelog", "remote_link"):
            text, attrs = gap_episode(row.kind, row)
        else:
            try:
                draft = await extract(row, no_lookup)
            except (Skip, Reject):
                skipped += 1
                continue
            text, attrs = draft.text, draft.attrs
        await add_episode(db, key, row.source, row.kind, row.occurred_at, text, attrs, row.id)
    return len(rows), skipped


async def ensure_node(db, source, label, key) -> int:
    """The node's id, created as a placeholder if its own episode has not loaded yet."""
    await db.execute(
        """insert into graph.nodes (uuid, tenant, source, label, key, placeholder) values ($1, $2, $3, $4, $5, true)
           on conflict (tenant, source, label, key) do nothing""",
        uuid.uuid5(NAMESPACE, f"{TENANT}|{source}|{label}|{key}"), TENANT, source, label, key)
    return await db.fetchval("select id from graph.nodes where (tenant, source, label, key) = ($1, $2, $3, $4)",
                             TENANT, source, label, key)


async def apply_node(db, ep, n) -> int:
    """Doc's ordering rules: newest wins, older only fills gaps, a delete clears content and ends its edges."""
    source = n.get("source", ep["source"])
    node_id = await ensure_node(db, source, n["label"], n["key"])
    cur = await db.fetchrow("select occurred_at, deleted, placeholder, props from graph.nodes where id = $1 for update", node_id)
    at = datetime.fromisoformat(n["occurred_at"].replace("Z", "+00:00")) if n.get("occurred_at") else ep["occurred_at"]
    newer = cur["occurred_at"] is None or at >= cur["occurred_at"]
    props = dict(n["props"])
    if props.pop("deleted", False):
        if newer:
            await db.execute("""update graph.nodes set deleted = true, placeholder = false, name = null, summary = null,
                                props = '{}', embed_text = null, embedding = null, occurred_at = $2 where id = $1""", node_id, at)
            await db.execute("""update graph.edges set invalid_at = $2, invalidated_by = $3
                                where (src = $1 or dst = $1) and invalid_at is null and expired_at is null""", node_id, at, ep["id"])
        return node_id
    if cur["deleted"]:
        return node_id
    name, embed = props.pop("name", None), props.pop("embed_text", None)
    old = json.loads(cur["props"])
    merged = {**old, **props} if newer else {**props, **old}
    keep_placeholder = n.get("placeholder", False) and cur["placeholder"]
    await db.execute(
        """update graph.nodes set props = $2::jsonb, placeholder = $3,
             name = case when $6 then coalesce($4, name) else coalesce(name, $4) end,
             embed_text = case when $6 then coalesce($5, embed_text) else coalesce(embed_text, $5) end,
             occurred_at = case when $6 and not $3 then $7 else occurred_at end where id = $1""",
        node_id, json.dumps(merged), keep_placeholder, name, embed, newer, at)
    return node_id


async def apply_edge(db, ep, e, touched: set[int]) -> None:
    src = await ensure_node(db, e.get("src_source", ep["source"]), e["src_label"], e["src_key"])
    dst = await ensure_node(db, e.get("dst_source", ep["source"]), e["dst_label"], e["dst_key"])
    touched |= {src, dst}
    at, invalid_at, invalidated_by = ep["occurred_at"], None, None
    owned = OWNED.get((ep["source"], ep["kind"]), {}).get(e["type"])
    if owned:
        # A newer fact of this type already holds: this one is history, ended by that fact. As Graphiti's
        # _apply_temporal_invalidation does, so a late arrival can never become current.
        main, other = (src, dst) if owned == "src" else (dst, src)
        far = "dst" if owned == "src" else "src"
        later = await db.fetchrow(
            f"""select valid_at, episode_id from graph.edges where {owned} = $1 and type = $2 and {far} <> $3
                and valid_at > $4 and invalid_at is null and expired_at is null order by valid_at limit 1""",
            main, e["type"], other, at)
        if later:
            invalid_at, invalidated_by = later["valid_at"], later["episode_id"]
    await db.execute(
        """insert into graph.edges (uuid, tenant, source, type, src, dst, fact, props, valid_at, invalid_at, episode_id, invalidated_by)
           values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
           on conflict do nothing""",
        uuid.uuid5(NAMESPACE, f"{src}|{e['type']}|{dst}|{at.isoformat()}"), TENANT, e.get("edge_source", ep["source"]), e["type"],
        src, dst, e.get("fact") or f"{e['src_key']} {e['type']} {e['dst_key']}", json.dumps(e.get("props") or {}), at, invalid_at,
        ep["id"], invalidated_by)


async def invalidate_missing(db, ep, attrs) -> None:
    """Doc's "edges that disappear": current owned edges this newer record no longer lists end at its time."""
    for edge_type, side in OWNED.get((ep["source"], ep["kind"]), {}).items():
        label, key = next(((e["src_label"], e["src_key"]) if side == "src" else (e["dst_label"], e["dst_key"])
                           for e in attrs["edges"] if e["type"] == edge_type), ("WorkItem", None))
        if key is None:
            key = next((n["key"] for n in attrs["nodes"] if n["label"] == "WorkItem"), None)
        if key is None:
            continue
        main = await ensure_node(db, ep["source"], label, key)
        far = "dst" if side == "src" else "src"
        listed = [e[f"{far}_key"] for e in attrs["edges"] if e["type"] == edge_type]
        await db.execute(
            f"""update graph.edges e set invalid_at = $3, invalidated_by = $4
                from graph.nodes other where other.id = e.{far} and e.{side} = $1 and e.type = $2
                and e.invalid_at is null and e.expired_at is null and e.valid_at <= $3 and other.key <> all($5::text[])""",
            main, edge_type, ep["occurred_at"], ep["id"], listed)


async def link(db, ep, node_ids: list[int]) -> None:
    """Doc's link step, in load's transaction: SAME_AS by email, and pull request URLs and Jira keys in message text."""
    rows = await db.fetch("select id, label, key, source, props, embed_text from graph.nodes where id = any($1)", node_ids)
    projects = [r["key"] for r in await db.fetch(
        "select key from graph.nodes where tenant = $1 and source = 'jira' and label = 'Container'", TENANT)]
    keys = re.compile(r"\b(?:%s)-\d+\b" % "|".join(map(re.escape, projects))) if projects else None
    for r in rows:
        email = json.loads(r["props"]).get("email")
        if r["label"] == "Person" and email:
            for other in await db.fetch(
                    """select key, source from graph.nodes where tenant = $1 and label = 'Person' and id < $2
                       and lower(props->>'email') = lower($3) and source <> $4""", TENANT, r["id"], email, r["source"]):
                await apply_edge(db, ep, {"type": "SAME_AS", "src_label": "Person", "src_key": r["key"], "src_source": r["source"],
                                          "dst_label": "Person", "dst_key": other["key"], "dst_source": other["source"],
                                          "edge_source": "link", "props": {"method": "email"}}, set())
        if r["label"] != "Message" or not r["embed_text"]:
            continue
        base = {"type": "REFERENCES", "src_label": "Message", "src_key": r["key"], "src_source": r["source"],
                "edge_source": "link", "props": {}}
        for repo, number in set(PULL_URL.findall(r["embed_text"])):
            await apply_edge(db, ep, {**base, "dst_label": "Change", "dst_key": f"{repo}#{number}", "dst_source": "github"}, set())
        for key in set(keys.findall(r["embed_text"])) if keys else ():
            await apply_edge(db, ep, {**base, "dst_label": "WorkItem", "dst_key": key, "dst_source": "jira"}, set())


async def apply_episode(db, ep) -> None:
    attrs = json.loads(ep["attrs"])
    node_ids = [await apply_node(db, ep, n) for n in attrs["nodes"]]
    touched: set[int] = set(node_ids)
    for e in attrs["edges"]:
        await apply_edge(db, ep, e, touched)
    await invalidate_missing(db, ep, attrs)
    await db.executemany("insert into graph.mentions values ($1, $2) on conflict do nothing",
                         [(node_id, ep["id"]) for node_id in touched])
    await link(db, ep, node_ids)


async def load(db) -> int:
    """Load's claim: one subgraph at a time under its advisory lock, oldest first, graph and loaded_at in one commit."""
    done = 0
    for (source,) in await db.fetch("select distinct source from episodes where tenant = $1 order by 1", TENANT):
        while True:
            async with db.transaction():
                await db.execute("select pg_advisory_xact_lock(hashtext($1))", f"{TENANT}_{source}")
                batch = await db.fetch(
                    """select id, source, kind, occurred_at, attrs from episodes where tenant = $1 and source = $2
                       and loaded_at is null order by occurred_at, id limit $3""", TENANT, source, LOAD_BATCH)
                for ep in batch:
                    await apply_episode(db, ep)
                await db.execute("update episodes set loaded_at = now() where id = any($1)", [ep["id"] for ep in batch])
            done += len(batch)
            if not batch:
                break
    return done


STATUS_AT = """
select s.name as status, e.valid_at, e.invalid_at, ep.key as episode
from graph.nodes wi
join graph.edges e on e.src = wi.id and e.type = 'HAS_STATUS'
join graph.nodes s on s.id = e.dst
join episodes ep on ep.id = e.episode_id
where (wi.tenant, wi.source, wi.label, wi.key) = ($1, 'jira', 'WorkItem', $2)
  and e.valid_at <= $3 and (e.invalid_at is null or e.invalid_at > $3) and e.expired_at is null
"""

# The change between two dates, the episode behind it, the last Jira comment before it, and each linked pull
# request's Slack message within 10 minutes. The comment and pull request rows are evidence by time and link: no
# field in the source says one caused the other.
WHY = """
with wi as (select id, props->>'url' as url from graph.nodes where (tenant, source, label, key) = ($1, 'jira', 'WorkItem', $2)),
change as (
  select e.valid_at, e.fact, ep.key, ep.attrs, ep.text, wi.url from wi
  join graph.edges e on e.src = wi.id and e.type = 'HAS_STATUS' and e.expired_at is null
  join episodes ep on ep.id = e.episode_id
  where e.valid_at > $3 and e.valid_at <= $4
)
select 1 as ord, 'changed by' as what, c.valid_at as at,
       c.text || ' (' || (select string_agg(i->>'field' || ': ' || coalesce(i->>'fromString', 'none') || ' -> ' || coalesce(i->>'toString', 'none'), '; ')
                          from jsonb_array_elements(c.attrs->'items') i) || ')' as detail,
       c.url, c.key as ref
from change c
union all
select * from (
  select 2, 'last comment', m.occurred_at, m.embed_text,
         sr.record->'payload'->'comment'->>'self', ep.key
  from change c, wi
  join graph.edges r on r.dst = wi.id and r.type = 'REFERENCES'
  join graph.nodes m on m.id = r.src and m.label = 'Message' and m.source = 'jira'
  join episodes ep on ep.id = r.episode_id
  join source_records sr on sr.id = ep.source_record_id
  where m.occurred_at <= c.valid_at
  order by m.occurred_at desc limit 1) comment
union all
select 3, 'pull request', msg.occurred_at, pr.name || ' | Slack: ' || msg.embed_text, pr.props->>'url', msg.key
from change c, wi
join graph.edges r on r.src = wi.id and r.type = 'REFERENCES'
join graph.nodes pr on pr.id = r.dst and pr.label = 'Change'
join graph.edges s on s.dst = pr.id and s.type = 'REFERENCES' and s.source = 'link'
join graph.nodes msg on msg.id = s.src and msg.label = 'Message' and msg.source = 'slack'
where msg.occurred_at between c.valid_at - interval '10 minutes' and c.valid_at + interval '10 minutes'
order by 1, 3
"""

# The doc's first answer to "what was the status": the newest episode that mentions the node, read from its attrs.
NODE_HISTORY = """
select n->'props'->>'status' from graph.nodes wi
join graph.mentions m on m.node_id = wi.id
join episodes ep on ep.id = m.episode_id, jsonb_array_elements(ep.attrs->'nodes') n
where (wi.tenant, wi.source, wi.label, wi.key) = ($1, 'jira', 'WorkItem', $2)
  and ep.occurred_at <= $3 and n->>'key' = wi.key and n->>'label' = 'WorkItem'
order by ep.occurred_at desc limit 1
"""

ASSIGNEE_AT = """
select p.name from graph.nodes wi
join graph.edges e on e.dst = wi.id and e.type = 'ASSIGNED_TO'
join graph.nodes p on p.id = e.src
where (wi.tenant, wi.source, wi.label, wi.key) = ($1, 'jira', 'WorkItem', $2)
  and e.valid_at <= $3 and (e.invalid_at is null or e.invalid_at > $3) and e.expired_at is null
"""


async def check_every_ticket(db) -> int:
    """Every ticket's status at each boundary matches the raw changelog, and an ended edge is never current."""
    checks = 0
    for key, (issue, page, _) in fixture_issues().items():
        history = status_history(issue, page)
        for since, status, _entry in history:
            for probe, expected in ((since, status), (since - timedelta(seconds=1), None)):
                rows = await db.fetch(STATUS_AT, TENANT, key, probe)
                if expected is None:
                    earlier = [s for t, s, _ in history if t <= probe]
                    expected = earlier[-1] if earlier else None
                assert [r["status"] for r in rows] == ([expected] if expected else []), (key, probe, rows)
                checks += 1
        now = await db.fetch(STATUS_AT, TENANT, key, datetime.now(timezone.utc))
        assert [r["status"] for r in now] == [issue["fields"]["status"]["name"]], (key, now)
    wrong = await db.fetchval(
        """select count(*) from (select e.src from graph.edges e where e.type = 'HAS_STATUS' and e.invalid_at is null
           and e.expired_at is null group by 1 having count(*) <> 1) x""")
    assert wrong == 0, f"{wrong} tickets without exactly one current status"
    leaked = await db.fetchval(
        "select count(*) from graph.edges where type = 'HAS_STATUS' and invalid_at is not null and invalidated_by is null")
    assert leaked == 0, f"{leaked} ended status edges do not say what ended them"
    return checks


class Rollback(Exception):
    pass


async def check_rules(db) -> None:
    """The rules photon has no real case for (it has no deletes, reassignments or late edits). The episodes here are
    made up and rolled back; nothing in the answer above comes from them."""
    try:
        async with db.transaction():
            async def run(kind, at, attrs):
                occurred = datetime.fromisoformat(at)
                id_ = await add_episode(db, f"rule:{kind}:{at}", "jira", kind, occurred, "rule check", attrs)
                await apply_episode(db, await db.fetchrow("select id, source, kind, occurred_at, attrs from episodes where id = $1", id_))

            def issue(person, status=None):
                edge = {"type": "ASSIGNED_TO", "src_label": "Person", "src_key": person, "dst_label": "WorkItem", "dst_key": "RULE-1", "props": {}}
                return {"nodes": [{"label": "WorkItem", "key": "RULE-1", "props": {"name": "rule", "status": status}}], "edges": [edge]}

            def assignees():
                return db.fetch("""select p.key from graph.edges e join graph.nodes wi on wi.id = e.dst join graph.nodes p on p.id = e.src
                                   where wi.key = 'RULE-1' and e.type = 'ASSIGNED_TO' and e.invalid_at is null""")

            await run("issue", "2026-01-02T00:00:00+00:00", issue("ana", "To Do"))
            await run("issue", "2026-01-04T00:00:00+00:00", issue("ben", "Done"))
            assert [r["key"] for r in await assignees()] == ["ben"], "reassigning must end the old edge"
            await run("issue", "2026-01-03T00:00:00+00:00", issue("cal", "Doing"))
            assert [r["key"] for r in await assignees()] == ["ben"], "a late older version must not become current"
            props = await db.fetchval("select props->>'status' from graph.nodes where key = 'RULE-1'")
            assert props == "Done", "a late older version must not overwrite newer props"
            await run("issue", "2026-01-05T00:00:00+00:00", {"nodes": [{"label": "WorkItem", "key": "RULE-1", "props": {"deleted": True}}], "edges": []})
            assert not await assignees(), "a delete must end the node's edges"
            await run("issue", "2026-01-04T12:00:00+00:00", issue("dee", "Back"))
            node = await db.fetchrow("select deleted, props, name from graph.nodes where key = 'RULE-1'")
            assert node["deleted"] and node["props"] == "{}" and node["name"] is None, "a late edit must not bring back a delete"
            raise Rollback
    except Rollback:
        pass


async def check_reapply(db) -> None:
    """Applying every loaded episode a second time, in order, changes no row (rolled back)."""
    state = """select (select count(*) from graph.nodes), (select count(*) from graph.edges),
                       (select count(*) from graph.edges where invalid_at is null), (select count(*) from graph.mentions)"""
    try:
        async with db.transaction():
            first = tuple(await db.fetchrow(state))
            for ep in await db.fetch("select id, source, kind, occurred_at, attrs from episodes where tenant = $1 order by occurred_at, id", TENANT):
                await apply_episode(db, ep)
            assert tuple(await db.fetchrow(state)) == first, "re-applying episodes must change nothing"
            raise Rollback
    except Rollback:
        pass


async def main() -> None:
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    await db.execute("delete from episodes where tenant = $1", TENANT)
    await db.execute("delete from source_records where tenant = $1", TENANT)
    await db.execute(SCHEMA)

    start = time.perf_counter()
    records, skipped = await seed_and_extract(db)
    extracted = time.perf_counter() - start
    start = time.perf_counter()
    loaded = await load(db)
    loaded_s = time.perf_counter() - start

    before, after = datetime.fromisoformat(BEFORE), datetime.fromisoformat(AFTER)
    start = time.perf_counter()
    at_before = await db.fetch(STATUS_AT, TENANT, TICKET, before)
    at_after = await db.fetch(STATUS_AT, TENANT, TICKET, after)
    why = await db.fetch(WHY, TENANT, TICKET, before, after)
    query_ms = (time.perf_counter() - start) * 1000

    print(f"{TICKET}: {await db.fetchval('select name from graph.nodes where key = $1', TICKET)}")
    print(f"  on {BEFORE}: {at_before[0]['status']} (since {at_before[0]['valid_at'].isoformat()})")
    print(f"  on {AFTER}: {at_after[0]['status']} (since {at_after[0]['valid_at'].isoformat()})")
    print("  why:")
    for row in why:
        print(f"    {row['what']}, {row['at'].isoformat()}: {' '.join((row['detail'] or '').split())[:230]}")
        print(f"      source: {row['url'] or 'none stored'}  ref: {row['ref']}")

    assert [r["status"] for r in at_before] == ["In Progress"]
    assert [r["status"] for r in at_after] == ["Done"]
    assert why and why[0]["what"] == "changed by" and "Ryan Stoffel" in why[0]["detail"]
    assert at_before[0]["invalid_at"] == at_after[0]["valid_at"], "the old fact must end when the new one starts"
    checks = await check_every_ticket(db)
    await check_rules(db)
    await check_reapply(db)
    print(f"\nself-check: {checks} status probes across {len(fixture_issues())} tickets match the raw changelog; "
          "rule and re-apply checks passed")

    print("\nwhat the design did not answer:")
    print(f"  doc's node history, status on {BEFORE}: {await db.fetchval(NODE_HISTORY, TENANT, TICKET, before)}")
    print(f"  ASSIGNED_TO on {BEFORE}: {await db.fetchval(ASSIGNEE_AT, TENANT, TICKET, before)}; on {AFTER}: {await db.fetchval(ASSIGNEE_AT, TENANT, TICKET, after)}")

    print("\nrows:")
    for name, sql in (("source_records", "select count(*) from source_records"), ("episodes", "select count(*) from episodes"),
                      ("graph.nodes", "select count(*) from graph.nodes"),
                      ("  placeholders", "select count(*) from graph.nodes where placeholder"),
                      ("graph.edges", "select count(*) from graph.edges"),
                      ("  ended", "select count(*) from graph.edges where invalid_at is not null"),
                      ("  link", "select count(*) from graph.edges where source = 'link'"),
                      ("  SAME_AS", "select count(*) from graph.edges where type = 'SAME_AS'"),
                      ("graph.mentions", "select count(*) from graph.mentions")):
        print(f"  {name}: {await db.fetchval(sql)}")
    print(f"\nseed + extract {extracted:.2f} s ({records} records, {skipped} skipped), load {loaded_s:.2f} s ({loaded} episodes), "
          f"status and why queries {query_ms:.1f} ms")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
