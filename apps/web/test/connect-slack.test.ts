// EMBER-148 acceptance (ADR 0010): an owner connects a Slack workspace from
// the web app through Slack's OAuth v2 install. The workspace's team_id maps
// to the tenant, the bot token is sealed as its `api` credential in the same
// transaction, deliveries stored while it was unmapped go to enrich, a
// workspace never moves to another tenant, and Disconnect uninstalls the app.
// Slack is a fake `fetch` here. The connection writes run on Postgres when
// TEST_POSTGRES_URL is set and the ingestion migrations are applied (dbmate up).
// Run: npm test            (no Postgres: those tests skip)
//      TEST_POSTGRES_URL=postgres://... npm test

import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";
import pg from "pg";
import { disconnectAccount } from "../src/lib/connections.ts";
import { seal as sealJira } from "../src/lib/jira-connect.ts";
import {
  asSlackCredential,
  authorizeUrl,
  BOT_SCOPES,
  connectWorkspace,
  isTeamId,
  matchState,
  missingScopes,
  seal,
  SlackClient,
  slackAppConfig,
  SlackError,
  uninstallWorkspace,
  type Install,
  type SlackAppConfig,
} from "../src/lib/slack-connect.ts";
import { KeyRing, loadCredential } from "../src/lib/source-credentials.ts";

const SECRET = "test-secret-at-least-32-characters-long!!";
const CONFIG: SlackAppConfig = { clientId: "111.222", clientSecret: "shh" };
const RING = KeyRing.parse("api", `test:${Buffer.alloc(32, 7).toString("base64")}`);

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** Slack as far as Connect Slack uses it: oauth.v2.access and apps.uninstall. */
function fakeSlack(answer: Record<string, unknown>, over: { uninstallError?: string } = {}) {
  const calls: { method: string; params: URLSearchParams }[] = [];
  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    const params = new URLSearchParams(String(init?.body ?? ""));
    const method = url.pathname.replace("/api/", "");
    calls.push({ method, params });
    assert.equal(init?.method, "POST");
    if (method === "oauth.v2.access") {
      if (params.get("client_secret") !== CONFIG.clientSecret) return json({ ok: false, error: "invalid_client_id" });
      if (params.get("code") !== "good-code") return json({ ok: false, error: "invalid_code" });
      return json({ ok: true, ...answer });
    }
    if (method === "apps.uninstall") {
      return json(over.uninstallError ? { ok: false, error: over.uninstallError } : { ok: true });
    }
    return json({ ok: false, error: "unknown_method" }, 404);
  }) as typeof fetch;
  return { client: new SlackClient(CONFIG, fetchImpl), calls };
}

function installAnswer(teamId: string, over: Record<string, unknown> = {}) {
  return {
    access_token: `xoxb-${teamId}-token`,
    token_type: "bot",
    scope: BOT_SCOPES.join(","),
    bot_user_id: "UEMBERBOT",
    app_id: "AEMBER",
    team: { id: teamId, name: "Acme" },
    enterprise: null,
    is_enterprise_install: false,
    authed_user: { id: "UOWNER" },
    ...over,
  };
}

describe("Slack app settings", () => {
  test("Connect Slack is unavailable until both halves of the OAuth client are set", () => {
    assert.equal(slackAppConfig({}), null);
    assert.equal(slackAppConfig({ SLACK_CLIENT_ID: "1.2" }), null);
    assert.deepEqual(slackAppConfig({ SLACK_CLIENT_ID: " 1.2 ", SLACK_CLIENT_SECRET: "s " }), { clientId: "1.2", clientSecret: "s" });
  });

  test("the install screen asks for exactly the manifest's bot scopes and no user scopes", () => {
    const url = new URL(authorizeUrl(CONFIG, "st", "https://ember.example/connect/slack/callback"));
    assert.equal(url.origin + url.pathname, "https://slack.com/oauth/v2/authorize");
    assert.equal(url.searchParams.get("client_id"), CONFIG.clientId);
    assert.deepEqual(url.searchParams.get("scope")!.split(","), [...BOT_SCOPES]);
    assert.equal(url.searchParams.get("user_scope"), null);
    assert.equal(url.searchParams.get("redirect_uri"), "https://ember.example/connect/slack/callback");
    assert.equal(url.searchParams.get("state"), "st");
  });

  test("the state cookie belongs to this browser, user and flow", () => {
    const cookie = seal({ state: "abc", organizationId: "org-1", userId: "u-1" }, SECRET, 60);
    assert.equal(matchState(cookie, "abc", "u-1", SECRET)?.organizationId, "org-1");
    assert.equal(matchState(cookie, "abd", "u-1", SECRET), null, "another state");
    assert.equal(matchState(cookie, "abc", "u-2", SECRET), null, "another user");
    assert.equal(matchState(cookie, "abc", "u-1", "another-secret-that-is-long-enough!!"), null);
    const jira = sealJira({ state: "abc", organizationId: "org-1", userId: "u-1" }, SECRET, 60);
    assert.equal(matchState(jira, "abc", "u-1", SECRET), null, "Connect Jira's cookie means nothing here");
    assert.equal(matchState(cookie, "abc", "u-1", SECRET, Date.now() + 61_000), null, "expired");
  });

  test("team ids are what intake reads from team_id, nothing else", () => {
    assert.ok(isTeamId("T0123ABCD"));
    for (const bad of ["t0123", "U0123ABCD", "T0123<b>", "", undefined, ["T0123ABCD"]]) assert.equal(isTeamId(bad), false);
  });
});

describe("Slack's API", () => {
  test("a code becomes the workspace and its bot token", async () => {
    const { client, calls } = fakeSlack(installAnswer("T0ACME"));
    const install = await client.exchangeCode("good-code", "https://ember.example/connect/slack/callback");
    assert.deepEqual(install, {
      teamId: "T0ACME",
      teamName: "Acme",
      botToken: "xoxb-T0ACME-token",
      botUserId: "UEMBERBOT",
      appId: "AEMBER",
      scope: BOT_SCOPES.join(","),
      installerId: "UOWNER",
      enterpriseId: null,
    });
    assert.equal(calls[0].params.get("redirect_uri"), "https://ember.example/connect/slack/callback");
    assert.equal(calls[0].params.get("client_id"), CONFIG.clientId);
  });

  test("a bad code, an org-wide Grid install, or no bot token is refused", async () => {
    await assert.rejects(fakeSlack(installAnswer("T0ACME")).client.exchangeCode("stale", "x"), (e: SlackError) => e.code === "invalid_code");
    const grid = fakeSlack(installAnswer("T0ACME", { team: null, is_enterprise_install: true, enterprise: { id: "E01" } }));
    await assert.rejects(grid.client.exchangeCode("good-code", "x"), (e: SlackError) => e.code === "enterprise_install");
    // The org-wide install Slack already made is taken back out with the token it gave
    const uninstall = grid.calls.find((c) => c.method === "apps.uninstall");
    assert.equal(uninstall?.params.get("token"), "xoxb-T0ACME-token");
    assert.equal(uninstall?.params.get("client_secret"), CONFIG.clientSecret);
    // If that fails too, the install is still refused, with the same reason
    const stuck = fakeSlack(installAnswer("T0ACME", { team: null, is_enterprise_install: true }), { uninstallError: "internal_error" });
    await assert.rejects(stuck.client.exchangeCode("good-code", "x"), (e: SlackError) => e.code === "enterprise_install");
    const user = fakeSlack(installAnswer("T0ACME", { token_type: "user", access_token: "xoxp-1" }));
    await assert.rejects(user.client.exchangeCode("good-code", "x"), (e: SlackError) => e.code === "no_bot_token");
    const noTeam = fakeSlack(installAnswer("T0ACME", { team: { name: "?" } }));
    await assert.rejects(noTeam.client.exchangeCode("good-code", "x"), (e: SlackError) => e.code === "no_team");
  });

  test("a workspace inside a Grid organization is a normal install", async () => {
    const install = await fakeSlack(installAnswer("T0GRID", { enterprise: { id: "E01" } })).client.exchangeCode("good-code", "x");
    assert.equal(install.teamId, "T0GRID");
    assert.equal(install.enterpriseId, "E01");
  });

  test("scopes Slack didn't grant are reported", () => {
    assert.deepEqual(missingScopes(BOT_SCOPES.join(",")), []);
    assert.deepEqual(missingScopes("channels:history,channels:read"), BOT_SCOPES.slice(2));
  });

  test("a stored credential that isn't Connect Slack's is refused", () => {
    assert.equal(asSlackCredential(null), null);
    assert.equal(asSlackCredential({ access_token: "xoxb-1" }), null);
    assert.equal(asSlackCredential({ bot_token: "xoxb-1", bot_user_id: "U1" })?.scope, "");
  });
});

const postgresUrl = process.env.TEST_POSTGRES_URL;

describe("Connect Slack on Postgres", { skip: postgresUrl ? false : "set TEST_POSTGRES_URL to run it" }, () => {
  let pool: pg.Pool;
  let ready = false;
  // Postgres keeps rows between runs, so team ids and tenants are unique to this run
  const run = Date.now().toString(36).toUpperCase();
  const tenant = (name: string) => `${name}-${run.toLowerCase()}`;
  let n = 0;

  before(async () => {
    pool = new pg.Pool({ connectionString: postgresUrl });
    const { rows } = await pool.query<{ ready: boolean }>("select to_regclass('public.source_credentials') is not null as ready");
    ready = rows[0]?.ready ?? false;
  });
  after(async () => {
    await pool?.end();
  });

  /** A workspace of its own for each test, installed as the callback would see it. */
  async function setup(over: Record<string, unknown> = {}, slack: Parameters<typeof fakeSlack>[1] = {}) {
    n++;
    const teamId = `T${run}${n}`;
    const fake = fakeSlack(installAnswer(teamId, over), slack);
    const install: Install = await fake.client.exchangeCode("good-code", "x");
    return { ...fake, install, teamId };
  }

  async function installation(teamId: string) {
    const { rows } = await pool.query(
      "select tenant, label, external_id, connected_by, disconnected_at from public.installations where source = 'slack' and account_id = $1",
      [teamId],
    );
    return rows[0];
  }

  async function storeDelivery(teamId: string, eventId: string) {
    await pool.query(
      `insert into public.raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)
       values ('slack', $1, $2, null, 'message', 'webhook', '{}'::jsonb)`,
      [`${teamId}:${eventId}`, teamId],
    );
  }

  test("connecting maps team_id to the tenant, stores the bot token, and hands over stored deliveries", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { install, teamId } = await setup();
    await storeDelivery(teamId, "Ev1");
    const result = await connectWorkspace(pool, RING, { install, tenant: tenant("acme"), userId: "u-1" });
    assert.deepEqual(result, { ok: true, reconnected: false });
    assert.deepEqual(await installation(teamId), { tenant: tenant("acme"), label: "Acme", external_id: null, connected_by: "u-1", disconnected_at: null });
    const credential = asSlackCredential(await loadCredential(pool, RING, { source: "slack", accountId: teamId }));
    assert.equal(credential?.bot_token, `xoxb-${teamId}-token`);
    assert.equal(credential?.bot_user_id, "UEMBERBOT");
    const { rows } = await pool.query("select tenant from public.raw_events where source = 'slack' and account_id = $1", [teamId]);
    assert.deepEqual(rows, [{ tenant: tenant("acme") }], "the stored delivery now has the tenant");
  });

  test("a workspace connected to another tenant is refused and its token is left alone", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { install, teamId } = await setup();
    await connectWorkspace(pool, RING, { install, tenant: tenant("acme"), userId: "u-1" });
    const before = await loadCredential(pool, RING, { source: "slack", accountId: teamId });
    const other = { ...install, botToken: "xoxb-other" };
    assert.deepEqual(await connectWorkspace(pool, RING, { install: other, tenant: tenant("other"), userId: "u-9" }), { ok: false, reason: "other-tenant" });
    assert.equal((await installation(teamId)).tenant, tenant("acme"));
    assert.deepEqual(await loadCredential(pool, RING, { source: "slack", accountId: teamId }), before);
  });

  test("reconnecting a disconnected workspace clears disconnected_at and replaces the token", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { install, teamId } = await setup();
    await connectWorkspace(pool, RING, { install, tenant: tenant("acme"), userId: "u-1" });
    await disconnectAccount(pool, { source: "slack", accountId: teamId, tenant: tenant("acme") });
    assert.equal(await loadCredential(pool, RING, { source: "slack", accountId: teamId }), null, "Disconnect deleted the token");
    const result = await connectWorkspace(pool, RING, { install: { ...install, botToken: "xoxb-new" }, tenant: tenant("acme"), userId: "u-2" });
    assert.deepEqual(result, { ok: true, reconnected: true });
    assert.equal((await installation(teamId)).disconnected_at, null);
    assert.equal(asSlackCredential(await loadCredential(pool, RING, { source: "slack", accountId: teamId }))?.bot_token, "xoxb-new");
  });

  test("Disconnect uninstalls the app with the stored token", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { install, teamId, client, calls } = await setup();
    await connectWorkspace(pool, RING, { install, tenant: tenant("acme"), userId: "u-1" });
    assert.equal(await uninstallWorkspace(pool, RING, client, teamId), "deleted");
    const call = calls.find((c) => c.method === "apps.uninstall");
    assert.equal(call?.params.get("token"), `xoxb-${teamId}-token`);
    assert.equal(call?.params.get("client_secret"), CONFIG.clientSecret);
    assert.equal(await uninstallWorkspace(pool, RING, client, "TNOBODY"), "none");
  });

  test("an app the workspace already removed counts as uninstalled; Slack being down doesn't", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const gone = await setup({}, { uninstallError: "invalid_auth" });
    await connectWorkspace(pool, RING, { install: gone.install, tenant: tenant("acme"), userId: "u-1" });
    assert.equal(await uninstallWorkspace(pool, RING, gone.client, gone.teamId), "deleted");
    const down = await setup({}, { uninstallError: "internal_error" });
    await connectWorkspace(pool, RING, { install: down.install, tenant: tenant("acme"), userId: "u-1" });
    assert.equal(await uninstallWorkspace(pool, RING, down.client, down.teamId), "failed");
  });
});
