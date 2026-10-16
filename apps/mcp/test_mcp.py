"""ember_mcp against a real Postgres (EMBER-79).

Needs psql, dbmate, the packages in requirements.txt, and a Postgres server it may create and drop
a database on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 apps/mcp/test_mcp.py

It builds the repo migrations in database ember_test_mcp.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from ember_mcp import retrieval, server  # noqa: E402

MIGRATIONS = REPO / "infra" / "k8s" / "apps" / "postgres" / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")
DB = "ember_test_mcp"
ISSUE_SELF = "https://photon.atlassian.net/rest/api/2/issue/10004"


def database_url() -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + DB))


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}{result.stderr}")


def jira_issue(summary: str, description: str) -> dict:
    return {"webhookEvent": "jira:issue_updated",
            "issue": {"id": "10004", "key": "PHO-4", "self": ISSUE_SELF,
                      "fields": {"summary": summary, "description": description}}}


# (source, kind, object_id, version, occurred_at day, payload), in the shapes the enrich workers store
ROWS = [
    ("github", "pull_request", "acme/photon#43", "2026-09-24T10:00:00Z", 24, {
        "action": "opened", "pull_request": {
            "number": 43, "title": "Cache app icons on first launch",
            "body": "Icons load from disk once and stay in memory.",
            "html_url": "https://github.com/acme/photon/pull/43"}}),
    ("jira", "issue", "10004", "v1", 20, jira_issue("Hotkey bug", "Something is off.")),
    ("jira", "issue", "10004", "v2", 22, jira_issue(
        "Global hotkey conflicts with Spotlight",
        "Pressing the global hotkey opens Spotlight instead of Photon. The hotkey conflict needs a new default.")),
    ("slack", "message", "C08DEV/1789353420.000100", "1789353420.000100", 23, {
        "channel": "C08DEV", "ts": "1789353420.000100", "user": "U1",
        "text": "Should we ship the icon cache before the release?"}),
    ("github", "commit", "acme/photon@" + "a" * 40, "a" * 40, 25, {
        "id": "a" * 40, "message": "Fix the default hotkey conflict with Spotlight\n\nSee PHO-4.",
        "url": "https://github.com/acme/photon/commit/" + "a" * 40}),
]


class McpTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", os.environ["PGTEST_URL"],
            "-c", f"drop database if exists {DB}", "-c", f"create database {DB}")
        run(DBMATE, "--url", database_url(), "--migrations-dir", str(MIGRATIONS), "--no-dump-schema", "migrate")
        os.environ["DATABASE_URL"] = database_url()

    @classmethod
    def tearDownClass(cls):
        run("psql", "-X", "-q", "-d", os.environ["PGTEST_URL"], "-c", f"drop database if exists {DB} with (force)")

    async def asyncSetUp(self):
        db = await asyncpg.connect(database_url())
        await db.execute("truncate source_records, episodes restart identity")
        await db.executemany(
            "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record)"
            " values ($1, 'acme', $2, $3, $4, $5, $6)",
            [(source, kind, object_id, version, datetime(2026, 9, day, tzinfo=timezone.utc), json.dumps({"payload": payload}))
             for source, kind, object_id, version, day, payload in ROWS])
        await db.close()

    async def test_search_ranks_the_relevant_record_first_and_filters_by_source(self):
        hits = await retrieval.search("spotlight hotkey conflict")
        self.assertEqual([(h["source"], h["object_id"]) for h in hits],
                         [("jira", "10004"), ("github", "acme/photon@" + "a" * 40)])
        github = await retrieval.search("spotlight hotkey conflict", sources=["github"])
        self.assertEqual([h["kind"] for h in github], ["commit"])
        icons = await retrieval.search("icon cache", sources=["slack", "jira"])
        self.assertEqual([h["source"] for h in icons], ["slack"])

    async def test_hits_carry_url_and_snippet_and_get_record_returns_the_newest_version(self):
        issue, commit = await retrieval.search("spotlight hotkey conflict")
        self.assertEqual(issue["url"], "https://photon.atlassian.net/browse/PHO-4")
        self.assertEqual(issue["title"], "PHO-4 Global hotkey conflicts with Spotlight")
        self.assertEqual(issue["version"], "v2")
        self.assertIn("**Spotlight**", issue["snippet"])
        self.assertEqual(commit["url"], "https://github.com/acme/photon/commit/" + "a" * 40)
        self.assertEqual(commit["title"], "Fix the default hotkey conflict with Spotlight")
        [pull] = await retrieval.search("icons memory", sources=["github"])
        self.assertEqual(pull["url"], "https://github.com/acme/photon/pull/43")

        record = await retrieval.get_record("jira", "10004")
        self.assertEqual(record["version"], "v2")
        self.assertEqual(record["record"]["payload"]["issue"]["fields"]["summary"], "Global hotkey conflicts with Spotlight")

    async def test_stdio_server_lists_and_calls_its_tools(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "ember_mcp"], cwd=HERE,
                                       env={"DATABASE_URL": database_url()})
        async with Client(params) as client:
            tools = await client.list_tools()
            self.assertEqual(sorted(t.name for t in tools.tools), ["get_record", "search"])
            result = await client.call_tool("search", {"question": "spotlight hotkey", "k": 1})
        self.assertFalse(result.is_error)
        [hit] = result.structured_content["result"]
        self.assertEqual((hit["source"], hit["object_id"]), ("jira", "10004"))

    async def test_sessions_are_read_only(self):
        conn = await retrieval.connect()
        try:
            with self.assertRaises(asyncpg.ReadOnlySQLTransactionError):
                await conn.execute("delete from source_records")
        finally:
            await conn.close()

    async def test_missing_database_url_is_a_tool_error(self):
        url = os.environ.pop("DATABASE_URL")
        try:
            async with Client(server.mcp) as client:
                result = await client.call_tool("search", {"question": "hotkey"})
        finally:
            os.environ["DATABASE_URL"] = url
        self.assertTrue(result.is_error)
        self.assertIn("DATABASE_URL is not set", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
