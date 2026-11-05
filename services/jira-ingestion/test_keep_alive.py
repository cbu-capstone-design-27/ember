"""keep_alive.py against a real Postgres built by the repo migrations (EMBER-131). Atlassian is stubbed.

Needs what test_enrich.py needs:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 services/jira-ingestion/test_keep_alive.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "packages" / "source-credentials"))

from sqlalchemy import text  # noqa: E402

import atlassian  # noqa: E402
import keep_alive  # noqa: E402
from enrich import make_engine  # noqa: E402
from test_enrich import CLIENT, CLOUD_ID, DBMATE, MIGRATIONS, RING, SITE, AtlassianStub, connect_site, run, stored_credential  # noqa: E402

TEST_DB = "ember_test_keep_alive"
OTHER_SITE = "second-site.atlassian.net"


class KeepAliveTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = urlunsplit(urlsplit(os.environ["PGTEST_URL"])._replace(path="/" + TEST_DB))
        dbmate = [DBMATE, "--url", cls.url, "--migrations-dir", str(MIGRATIONS), "--no-dump-schema"]
        run(*dbmate, "drop")
        run(*dbmate, "up")

    @classmethod
    def tearDownClass(cls):
        run(DBMATE, "--url", cls.url, "drop")

    async def asyncSetUp(self):
        self.engine = make_engine(self.url, pool_size=2)
        async with self.engine.begin() as conn:
            await conn.execute(text("truncate installations cascade"))

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def keep_alive(self, stub: AtlassianStub) -> int:
        return await keep_alive.keep_alive(atlassian.SiteCredentials(self.engine, RING, CLIENT, exchange=stub))

    async def scalar(self, sql: str, **params):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(sql), params)

    async def test_refreshes_the_token_and_extends_the_webhooks(self):
        await connect_site(self.engine, webhook_ids=[1001, 1002])   # an hour left: refreshed anyway
        stub = AtlassianStub()
        self.assertEqual(await self.keep_alive(stub), 0)

        api = atlassian.api_origin(CLOUD_ID)
        self.assertEqual(stub.urls(), [f"POST {atlassian.TOKEN_URL}", f"PUT {api}/rest/api/3/webhook/refresh"])
        _, _, headers, body = stub.calls[1]
        self.assertEqual(headers["Authorization"], "Bearer access-1")
        self.assertEqual(json.loads(body), {"webhookIds": [1001, 1002]})
        self.assertEqual((await stored_credential(self.engine))["refresh_token"], "refresh-1")

    async def test_a_refused_refresh_disconnects_the_site(self):
        await connect_site(self.engine)
        stub = AtlassianStub(token_error="invalid_grant")
        self.assertEqual(await self.keep_alive(stub), 0)

        self.assertEqual(stub.urls(), [f"POST {atlassian.TOKEN_URL}"])
        self.assertIsNotNone(await self.scalar("select disconnected_at from installations where account_id = :a", a=SITE))
        self.assertIsNone(await stored_credential(self.engine))
        # The next run has nothing to do
        stub = AtlassianStub()
        self.assertEqual(await self.keep_alive(stub), 0)
        self.assertEqual(stub.calls, [])

    async def test_other_failures_count_and_leave_the_site_connected(self):
        await connect_site(self.engine)
        await connect_site(self.engine, site=OTHER_SITE, tenant="other-tenant")

        self.assertEqual(await self.keep_alive(AtlassianStub(refresh_status=404)), 2)
        # A refusal that isn't invalid_grant (our client, say) is ours to fix, not the owner's
        self.assertEqual(await self.keep_alive(AtlassianStub(token_error="unauthorized_client")), 2)
        self.assertEqual(await self.scalar("select count(*) from installations where disconnected_at is null"), 2)
        self.assertIsNotNone(await stored_credential(self.engine, OTHER_SITE))

    async def test_main_needs_the_ring_and_the_client(self):
        for missing in ("SOURCE_CREDENTIALS_API_KEYS", "JIRA_OAUTH_CLIENT_SECRET"):
            env = {"DATABASE_URL": self.url, "SOURCE_CREDENTIALS_API_KEYS": "test:" + "A" * 43 + "=",
                   "JIRA_OAUTH_CLIENT_ID": "id", "JIRA_OAUTH_CLIENT_SECRET": "secret"}
            del env[missing]
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(keep_alive.main(exchange=AtlassianStub()), 2)


if __name__ == "__main__":
    unittest.main()
