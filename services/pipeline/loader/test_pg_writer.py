"""The Postgres graph writer (EMBER-164) against a real Postgres. Skipped unless PGTEST_URL is set.

Needs dbmate and a Postgres server with pgvector it may create and drop a database on (CI's "Pipeline
loader against Postgres" step), from services/pipeline:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \\
        python -m unittest loader.test_pg_writer -v

It builds the repo migrations in database ember_test_pg_writer, then runs extract and load with
GRAPH_STORE=postgres's writer.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

import asyncpg

from loader.pg_writer import PgWriter, _shorten
from loader.records import UUID_NAMESPACE, episode_uuid
from loader.test_pg_load import DBMATE, MIGRATIONS, PGTEST_URL, REPO, database_url, photon_rows
from loader.workers import ExtractWorker, LoadWorker

DB = "ember_test_pg_writer"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SITE = "https://acme.atlassian.net"

# Everything load wrote, for "a rerun changes nothing"
STATE = """
select (select json_agg(g order by g.id) from (select id, tenant, source, scope, group_id from graph.graphs) g)::text,
       (select json_agg(n order by n.uuid) from (select uuid, graph_id, tenant, label, key, name, props, placeholder,
               deleted, occurred_at, embed_text from graph.nodes) n)::text,
       (select json_agg(e order by e.uuid) from (select uuid, graph_id, tenant, type, fact, props, valid_at, invalid_at,
               episode_id, invalidated_by from graph.edges) e)::text,
       (select json_agg(m order by m.node_id, m.episode_id) from graph.mentions m)::text
"""


def jira_issue(key: str, *, assignee: str | None = None, status: str = "To Do", summary: str = "Fix login",
               moved: bool = False) -> dict:
    """A jira:issue_updated record as enrich:jira stores it. moved: the changelog changes its status."""
    number = key.rpartition("-")[2]
    issue = {"id": f"20{number}", "key": key, "self": f"{SITE}/rest/api/3/issue/20{number}",
             "fields": {"summary": summary, "project": {"key": key.rpartition("-")[0], "name": "Acme"},
                        "status": {"name": status}, "created": "2026-09-01T09:00:00.000+0000"}}
    if assignee:
        issue["fields"]["assignee"] = {"accountId": assignee, "displayName": assignee.title(),
                                       "emailAddress": f"{assignee}@acme.example"}
    payload = {"webhookEvent": "jira:issue_updated", "issue": issue}
    if moved:
        payload["changelog"] = {"items": [{"field": "status", "fromString": "?", "toString": status}]}
    return {"payload": payload}


def reconciled_issue(key: str, histories: list[dict], **kwargs) -> dict:
    """The issue as Jira reconcile (EMBER-167) stores it: the webhook shape with its whole changelog nested."""
    record = jira_issue(key, **kwargs)
    record["payload"]["issue"]["changelog"] = {"histories": histories}
    return record


def history(id_: str, at: datetime, *items: tuple) -> dict:
    """One changelog entry; items are (field, from, from name, to, to name)."""
    return {"id": id_, "created": at.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "items": [{"field": f, "from": a, "fromString": an, "to": b, "toString": bn} for f, a, an, b, bn in items]}


def github_issue(repo: str, number: int, login: str, body: str = "") -> dict:
    issue = {"number": number, "title": f"Issue {number}", "body": body, "state": "open", "user": {"login": login},
             "assignees": [], "html_url": f"https://github.com/{repo}/issues/{number}",
             "created_at": "2026-10-01T12:00:00Z", "updated_at": "2026-10-01T12:00:00Z"}
    return {"payload": {"action": "opened", "issue": issue, "repository": {"full_name": repo}}}


def slack_message(ts: str, text: str, team: str = "T1") -> dict:
    return {"payload": {"type": "message", "channel": "C1", "ts": ts, "user": "U1", "text": text, "team": team}}


@unittest.skipUnless(PGTEST_URL, "PGTEST_URL not set")
class PgWriterTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        async def recreate():
            conn = await asyncpg.connect(PGTEST_URL)
            try:
                await conn.execute(f"drop database if exists {DB} with (force)")
                await conn.execute(f"create database {DB}")
            finally:
                await conn.close()

        asyncio.run(recreate())
        result = subprocess.run([DBMATE, "--url", database_url(DB), "--migrations-dir", str(MIGRATIONS),
                                 "--no-dump-schema", "migrate"], capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"dbmate failed:\n{result.stdout}{result.stderr}")

    @classmethod
    def tearDownClass(cls):
        async def drop():
            conn = await asyncpg.connect(PGTEST_URL)
            try:
                await conn.execute(f"drop database if exists {DB} with (force)")
            finally:
                await conn.close()

        asyncio.run(drop())

    async def asyncSetUp(self):
        self.db = await asyncpg.connect(database_url(DB))
        await self.db.execute("truncate graph.graphs, source_records, episodes restart identity cascade")

    async def asyncTearDown(self):
        await self.db.close()

    async def insert(self, tenant, source, kind, object_id, version, occurred_at, record) -> int:
        return await self.db.fetchval(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, $2, $3, $4, $5, $6, $7::jsonb) returning id",
            source, tenant, kind, object_id, version, occurred_at, json.dumps(record))

    async def run_pipeline(self, batch: int = 100) -> LoadWorker:
        await ExtractWorker(database_url(DB)).drain()
        load = LoadWorker(database_url(DB), PgWriter(), batch=batch)
        await load.drain()
        self.assertEqual(await self.db.fetchval("select count(*) from episodes where loaded_at is null"), 0,
                         dict(load.counts))
        return load

    async def node(self, tenant: str, label: str, key: str):
        return await self.db.fetchrow("select * from graph.nodes where (tenant, label, key) = ($1, $2, $3)",
                                      tenant, label, key)

    async def current(self, type_: str, dst_or_src_key: str) -> list[str]:
        """Keys at the other end of the current edges of one type touching a node."""
        rows = await self.db.fetch(
            """select case when s.key = $2 then d.key else s.key end as key from graph.edges e
               join graph.nodes s on s.id = e.src join graph.nodes d on d.id = e.dst
               where e.type = $1 and $2 in (s.key, d.key) and e.invalid_at is null order by 1""", type_, dst_or_src_key)
        return [r["key"] for r in rows]

    # --- episodes.uuid ---

    async def test_the_migration_backfill_and_extract_give_an_episode_the_same_uuid(self):
        migration = (MIGRATIONS / "20261112120000_graph_tables.sql").read_text()
        [literal] = re.findall(r"uuid_generate_v5\('([0-9a-f-]+)', key\)", migration)
        self.assertEqual(literal, str(UUID_NAMESPACE), "the migration's namespace is not loader/records.py's")
        key = "photon:jira:issue:10001@2026-09-25T10:00:00.000+0000"
        self.assertEqual(str(await self.db.fetchval("select uuid_generate_v5($1::uuid, $2)", literal, key)),
                         episode_uuid(key))
        await self.insert("acme", "slack", "message", "C1/1789353000.000100", "1", T0,
                          slack_message("1789353000.000100", "hello"))
        await ExtractWorker(database_url(DB)).drain()
        row = await self.db.fetchrow(f"select uuid, name, key, uuid_generate_v5('{literal}', key) as backfill from episodes")
        self.assertEqual((str(row["uuid"]), row["name"]), (episode_uuid(row["key"]), row["key"]))
        self.assertEqual(row["uuid"], row["backfill"])

    # --- photon ---

    async def test_photon_loads_into_one_graph_per_scope_and_a_rerun_changes_nothing(self):
        rows = photon_rows()
        await self.db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, 'photon', $2, $3, $4, $5, $6::jsonb)",
            [(s, k, o, v, at, json.dumps({"payload": p})) for s, k, o, v, at, p in rows])
        await self.run_pipeline(batch=100)  # several batches per graph

        graphs = await self.db.fetch("select id, source, scope, group_id from graph.graphs order by source")
        self.assertEqual([(g["source"], g["scope"]) for g in graphs],
                         [("jira", "photon-synthetic.atlassian.net/PHO"), ("slack", "T08PHOTON01")])
        for g in graphs:
            self.assertEqual(g["group_id"], f"photon_{g['source']}_{g['id']}")
        self.assertEqual(await self.db.fetchval("select count(*) from episodes where graph_id is null"), 0)

        counts = {(r["source"], r["label"]): r["n"] for r in await self.db.fetch(
            """select g.source, n.label, count(*) as n from graph.nodes n join graph.graphs g on g.id = n.graph_id
               where not n.placeholder group by 1, 2""")}
        issues = sum(1 for r in rows if r[:2] == ("jira", "issue"))
        self.assertEqual(counts[("jira", "WorkItem")], issues)
        self.assertEqual(counts[("jira", "Message")], sum(1 for r in rows if r[:2] == ("jira", "comment")))
        self.assertEqual(counts[("slack", "Message")], len({r[2] for r in rows if r[0] == "slack"}))
        self.assertEqual(await self.db.fetchval("select count(*) from graph.nodes where placeholder"), 0)
        # every edge inside a graph joins two nodes of that graph
        self.assertEqual(await self.db.fetchval(
            """select count(*) from graph.edges e join graph.nodes s on s.id = e.src join graph.nodes d on d.id = e.dst
               where e.graph_id is not null and (s.graph_id <> e.graph_id or d.graph_id <> e.graph_id)"""), 0)
        # one current status per ticket, known only from its snapshot
        self.assertEqual(await self.db.fetchval(
            """select count(distinct src) from graph.edges where type = 'HAS_STATUS' and invalid_at is null
               and props->>'valid_from' = 'snapshot'"""), issues)
        self.assertEqual(await self.db.fetchval(
            "select count(*) from (select src from graph.edges where type = 'HAS_STATUS' and invalid_at is null"
            " group by 1 having count(*) > 1) x"), 0)
        # Slack messages naming a PHO key reference the ticket in the Jira graph
        jira_id = graphs[0]["id"]
        links = await self.db.fetch(
            """select s.graph_id as from_graph, d.graph_id as to_graph, e.props->>'method' as method, d.label
               from graph.edges e join graph.nodes s on s.id = e.src join graph.nodes d on d.id = e.dst
               where e.graph_id is null""")
        self.assertTrue(links)
        self.assertEqual({(r["from_graph"], r["to_graph"], r["method"], r["label"]) for r in links},
                         {(graphs[1]["id"], jira_id, "jira_key", "WorkItem")})
        self.assertIn("PHO-", await self.db.fetchval(
            "select fact from graph.edges where graph_id is null and type = 'REFERENCES' limit 1"))

        before = await self.db.fetchrow(STATE)
        await self.db.execute("update episodes set loaded_at = null")
        load = await self.run_pipeline()
        self.assertEqual(load.counts["loaded"], len(rows))
        self.assertEqual(await self.db.fetchrow(STATE), before, "re-running load changed the graph")

    async def test_photon_facts_read_as_text_with_names_not_markup_or_ids(self):
        """Ryan's three facts from the live test, with Slack's users and channels loaded after their messages."""
        rows = photon_rows()
        await self.db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, 'photon', $2, $3, $4, $5, $6::jsonb)",
            [(s, k, o, v, at, json.dumps({"payload": p})) for s, k, o, v, at, p in rows])
        later = max(r[4] for r in rows) + timedelta(days=1)
        for line in (REPO / "services/pipeline/fixtures/photon/slack-intake.jsonl").read_text().splitlines():
            body = json.loads(line)["body"]
            kind = "user" if "real_name" in body else "channel" if "context_team_id" in body else None
            if kind:  # as enrich:slack stores them: the object itself
                await self.insert("photon", "slack", kind, body["id"], "1", later, body)
        await self.run_pipeline()

        async def fact(type_: str, text: str) -> str:
            return await self.db.fetchval(
                """select e.fact from graph.edges e join graph.nodes m on m.id in (e.src, e.dst)
                   where e.type = $1 and m.label = 'Message' and m.embed_text like $2""", type_, f"%{text}%")

        pho87 = await self.db.fetchval("select name from graph.nodes where key = 'PHO-87'")
        self.assertEqual(await fact("REFERENCES", "browse/PHO-87|"),
                         f"Daniel Reyes created Bug PHO-87: fix: Caps Lock Hyper key must not toggle Caps Lock "
                         f"references {pho87}")
        text = await self.db.fetchval("select embed_text from graph.nodes where embed_text like 'Caps Lock as Hyper%'")
        authored = await fact("AUTHORED", "Caps Lock as Hyper was turning")
        self.assertEqual(authored, f"Ryan Stoffel authored {_shorten(text)}")
        self.assertTrue(authored.endswith("…") and len(_shorten(text)) <= 121, authored)
        self.assertEqual(await fact("PART_OF", "browse/PHO-20|"),
                         "Daniel Reyes created Bug PHO-20: Launcher panel throws NSInternalInconsistencyException"
                         " at launch is part of #photon-bugs")
        self.assertEqual(await self.db.fetchval(
            r"select count(*) from graph.edges where fact ~ '<https?:|<[@#!]|slack:|C08[A-Z0-9]+:\d'"), 0)

    # --- tenancy and links ---

    async def test_two_repos_are_two_graphs_joined_by_same_as_and_links_stay_in_the_tenant(self):
        for tenant in ("acme", "other"):
            for repo in ("acme/api", "acme/web"):
                await self.insert(tenant, "github", "issue", f"{repo}#1", "1", T0, github_issue(repo, 1, "ada"))
        await self.insert("acme", "jira", "issue", "201", "1", T0, jira_issue("ACME-1", assignee="bo"))
        await self.insert("acme", "slack", "user", "U1", "1", T0,
                          {"id": "U1", "team_id": "T1", "name": "bo", "profile": {"email": "BO@acme.example"}})
        await self.run_pipeline()

        scopes = await self.db.fetch("select tenant, scope from graph.graphs where source = 'github' order by 1, 2")
        self.assertEqual([tuple(r) for r in scopes], [("acme", "acme/api"), ("acme", "acme/web"),
                                                      ("other", "acme/api"), ("other", "acme/web")])
        same_as = await self.db.fetch(
            """select e.tenant, e.graph_id, e.props->>'method' as method, s.key as src, d.key as dst,
                      s.tenant as src_tenant, d.tenant as dst_tenant, s.graph_id <> d.graph_id as across
               from graph.edges e join graph.nodes s on s.id = e.src join graph.nodes d on d.id = e.dst
               where e.type = 'SAME_AS' order by 1, 3""")
        self.assertEqual({(r["tenant"], r["method"], *sorted([r["src"], r["dst"]])) for r in same_as},
                         {("acme", "source_id", "github:ada", "github:ada"), ("acme", "email", "jira:bo", "slack:U1"),
                          ("other", "source_id", "github:ada", "github:ada")})
        self.assertEqual(len(same_as), 3)
        for r in same_as:
            self.assertIsNone(r["graph_id"])
            self.assertTrue(r["across"])
            self.assertEqual((r["src_tenant"], r["dst_tenant"]), (r["tenant"], r["tenant"]), "a link crossed tenants")

    # --- newest wins, tombstones and invalidation ---

    async def test_newest_wins_edges_are_invalidated_and_a_tombstone_ends_everything(self):
        async def load(version, minutes, record, deleted=False):
            if deleted:
                record = {**record, "deleted": True}
            await self.insert("acme", "jira", "issue", "201", version, T0 + timedelta(minutes=minutes), record)
            await self.run_pipeline()
            return await self.db.fetchval("select max(id) from episodes")

        await load("1", 0, jira_issue("ACME-1", assignee="ana", status="To Do", summary="v1"))
        reassigned = await load("3", 30, jira_issue("ACME-1", assignee="ben", status="Done", summary="v3", moved=True))
        await load("2", 15, jira_issue("ACME-1", assignee="cal", status="Doing", summary="v2"))  # late and older

        work = await self.node("acme", "WorkItem", "ACME-1")
        self.assertEqual((work["name"], json.loads(work["props"])["status"]), ("v3", "Done"), "an older version won")
        self.assertEqual(await self.current("ASSIGNED_TO", "ACME-1"), ["jira:ben"])
        self.assertEqual(await self.current("HAS_STATUS", "ACME-1"), ["ACME:Done"])
        ended = {r["key"]: r for r in await self.db.fetch(
            """select p.key, e.valid_at, e.invalid_at, e.invalidated_by from graph.edges e
               join graph.nodes p on p.id = e.src where e.type = 'ASSIGNED_TO'""")}
        self.assertEqual((ended["jira:ana"]["invalid_at"], ended["jira:ana"]["invalidated_by"]),
                         (T0 + timedelta(minutes=30), reassigned), "the reassignment must end ana's edge")
        self.assertEqual(ended["jira:cal"]["invalid_at"], T0 + timedelta(minutes=30),
                         "a late, older version is stored already ended")
        self.assertEqual(await self.db.fetchval(
            "select e.props->>'valid_from' from graph.edges e join graph.nodes s on s.id = e.dst"
            " where s.key = 'ACME:Done'"), None, "a changelog status change is not a snapshot")

        deleted = await load("deleted:1", 45, jira_issue("ACME-1"), deleted=True)
        late = await load("4", 40, jira_issue("ACME-1", assignee="dee", summary="late edit"))
        work = await self.node("acme", "WorkItem", "ACME-1")
        self.assertTrue(work["deleted"])
        self.assertIsNone(work["name"])
        self.assertEqual(json.loads(work["props"]), {"source": "jira", "kind": "issue", "external_id": "ACME-1"})
        self.assertEqual(await self.db.fetchval(
            """select count(*) from graph.edges e join graph.nodes w on w.id in (e.src, e.dst)
               where w.key = 'ACME-1' and e.invalid_at is null"""), 0, "a delete must end the node's edges")
        ended = await self.db.fetch(
            """select e.type, e.episode_id = $3 as late from graph.edges e join graph.nodes w on w.id in (e.src, e.dst)
               where w.key = 'ACME-1' and e.invalid_at = $1 and e.invalidated_by = $2 order by 1, 2""",
            T0 + timedelta(minutes=45), deleted, late)
        # ben's assignment, Done and CONTAINS end with the delete; the late edit's facts are stored ended by it
        self.assertEqual([(r["type"], r["late"]) for r in ended],
                         [("ASSIGNED_TO", False), ("ASSIGNED_TO", True), ("CONTAINS", False), ("CONTAINS", True),
                          ("HAS_STATUS", False), ("HAS_STATUS", True)])

    async def facts(self, type_: str) -> list[tuple]:
        """(other end's key, valid_at, invalid_at, invalidated_by) of one type's edges around ACME-1, oldest first."""
        return [tuple(r) for r in await self.db.fetch(
            """select case when s.key = 'ACME-1' then d.key else s.key end, e.valid_at, e.invalid_at, e.invalidated_by
               from graph.edges e join graph.nodes s on s.id = e.src join graph.nodes d on d.id = e.dst
               where e.type = $1 and 'ACME-1' in (s.key, d.key) order by e.valid_at""", type_)]

    async def test_a_reconciled_changelog_loads_one_fact_per_status_and_assignee_held(self):
        created, t1, t2 = datetime(2026, 9, 1, 9, tzinfo=timezone.utc), T0, T0 + timedelta(days=5)
        record = reconciled_issue("ACME-1", [
            history("2", t2, ("status", "3", "In Progress", "4", "Done")),
            history("1", t1, ("status", "1", "To Do", "3", "In Progress"), ("assignee", "ana", "Ana", "ben", "Ben")),
        ], status="Done", assignee="ben")
        await self.insert("acme", "jira", "issue", "201", "1", t2, record)
        await self.run_pipeline()
        episode = await self.db.fetchval("select id from episodes")

        self.assertEqual(await self.facts("HAS_STATUS"), [("ACME:To Do", created, t1, episode),
                                                          ("ACME:In Progress", t1, t2, episode),
                                                          ("ACME:Done", t2, None, None)])
        self.assertEqual(await self.facts("ASSIGNED_TO"), [("jira:ana", created, t1, episode),
                                                           ("jira:ben", t1, None, None)])
        ana = await self.node("acme", "Person", "jira:ana")
        self.assertEqual((ana["name"], ana["placeholder"]), ("Ana", False))

        before = await self.db.fetchrow(STATE)
        await self.db.execute("update episodes set loaded_at = null")
        await self.run_pipeline()
        self.assertEqual(await self.db.fetchrow(STATE), before, "re-running load changed the graph")

    async def test_a_webhook_change_and_its_reconciled_history_make_one_fact(self):
        t1, t2 = T0, T0 + timedelta(days=5)
        await self.insert("acme", "jira", "issue", "201", "webhook", t2,
                          jira_issue("ACME-1", status="Done", moved=True))
        await self.run_pipeline()
        await self.insert("acme", "jira", "issue", "201", "reconcile", t2, reconciled_issue("ACME-1", [
            history("1", t1, ("status", "1", "To Do", "3", "In Progress")),
            history("2", t2, ("status", "3", "In Progress", "4", "Done"))], status="Done"))
        await self.run_pipeline()
        self.assertEqual([(k, v, i) for k, v, i, _ in await self.facts("HAS_STATUS")],
                         [("ACME:To Do", datetime(2026, 9, 1, 9, tzinfo=timezone.utc), t1),
                          ("ACME:In Progress", t1, t2), ("ACME:Done", t2, None)])

    async def test_an_edge_to_a_node_not_loaded_yet_makes_a_placeholder_its_episode_fills(self):
        comment = {"payload": {"webhookEvent": "comment_created", "issue": {"id": "201", "key": "ACME-1",
                                                                            "self": f"{SITE}/rest/api/3/issue/201"},
                               "comment": {"id": "9", "self": f"{SITE}/rest/api/3/issue/201/comment/9",
                                           "body": "On it", "created": "2026-10-01T12:00:00.000+0000"}}}
        await self.insert("acme", "jira", "comment", "201/comment/9", "1", T0, comment)
        await self.run_pipeline()
        self.assertTrue((await self.node("acme", "WorkItem", "ACME-1"))["placeholder"])
        await self.insert("acme", "jira", "issue", "201", "1", T0 + timedelta(minutes=1), jira_issue("ACME-1"))
        await self.run_pipeline()
        work = await self.node("acme", "WorkItem", "ACME-1")
        self.assertEqual((work["placeholder"], work["name"]), (False, "Fix login"))
        self.assertEqual(await self.db.fetchval("select count(*) from graph.nodes where label = 'WorkItem'"), 1)

    # --- row-level security ---

    async def test_a_reader_sees_only_its_tenants_and_nothing_when_unset(self):
        for tenant in ("a", "b"):
            await self.insert(tenant, "slack", "message", "C1/1789353000.000100", "1", T0,
                              slack_message("1789353000.000100", f"hello from {tenant}"))
        await self.run_pipeline()
        tables = ("graph.graphs", "graph.nodes", "graph.edges", "graph.mentions", "episodes", "source_records")
        total = {t: await self.db.fetchval(f"select count(*) from {t}") for t in tables}
        self.assertTrue(all(total.values()), total)

        async def seen(tenants: str | None) -> dict[str, int]:
            async with self.db.transaction():
                await self.db.execute("set local role ember_graph_reader")
                if tenants is not None:
                    await self.db.execute("select set_config('ember.tenants', $1, true)", tenants)
                return {t: await self.db.fetchval(f"select count(*) from {t}") for t in tables}

        self.assertEqual(await seen(None), dict.fromkeys(tables, 0))
        self.assertEqual(await seen(""), dict.fromkeys(tables, 0))
        self.assertEqual(await seen("a,b"), total)
        only_a = await seen("a")
        self.assertEqual({t: n * 2 for t, n in only_a.items()}, total)  # the two tenants hold the same shape
        async with self.db.transaction():
            await self.db.execute("set local role ember_graph_reader")
            await self.db.execute("select set_config('ember.tenants', 'a', true)")
            for table in ("graph.graphs", "graph.nodes", "graph.edges", "episodes", "source_records"):
                self.assertEqual(await self.db.fetchval(f"select count(*) from {table} where tenant = 'b'"), 0)
            self.assertEqual(await self.db.fetchval(
                "select count(*) from graph.mentions m join episodes e on e.id = m.episode_id where e.tenant = 'b'"), 0)

        # Any other role (the workers, the metrics exporter) keeps seeing every row of source_records
        await self.db.execute("""do $$ begin
            if not exists (select from pg_roles where rolname = 'it_other_reader') then
              create role it_other_reader nologin; end if; end $$""")
        await self.db.execute("grant select on source_records, episodes to it_other_reader")
        async with self.db.transaction():
            await self.db.execute("set local role it_other_reader")
            self.assertEqual(await self.db.fetchval("select count(*) from source_records"), total["source_records"])
            self.assertEqual(await self.db.fetchval("select count(*) from episodes"), total["episodes"])


if __name__ == "__main__":
    unittest.main()
