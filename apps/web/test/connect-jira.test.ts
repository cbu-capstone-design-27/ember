// EMBER-131 acceptance (ADR 0010): an owner connects a Jira site from the web
// app through Atlassian's 3LO consent. Only sites the consent covers are
// offered, the webhook is filtered to the projects they tick, the token pair
// is sealed as the site's `api` credential, a site never moves to another
// tenant, and Disconnect deletes the webhook. Atlassian is a fake `fetch`
// here. The connection writes run on Postgres when TEST_POSTGRES_URL is set
// and the ingestion migrations are applied (dbmate up).
// Run: npm test            (no Postgres: those tests skip)
//      TEST_POSTGRES_URL=postgres://... npm test

import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";
import pg from "pg";
import { seal as sealGitHub } from "../src/lib/github-connect.ts";
import {
  AtlassianClient,
  AtlassianError,
  asJiraCredential,
  authorizeUrl,
  connectSite,
  deletePending,
  freshJiraCredential,
  jiraAppConfig,
  loadPending,
  matchState,
  MAX_PROJECTS,
  projectJql,
  removeWebhooks,
  savePending,
  seal,
  unseal,
  WEBHOOK_EVENTS,
  type JiraAppConfig,
  type PendingConsent,
  type PendingSite,
  type VerificationStore,
} from "../src/lib/jira-connect.ts";
import { KeyRing, loadCredential } from "../src/lib/source-credentials.ts";

const SECRET = "test-secret-at-least-32-characters-long!!";
const CONFIG: JiraAppConfig = { clientId: "atl-client", clientSecret: "shh", webhookUrl: "https://ember.example/webhook/jira" };
const RING = KeyRing.parse("api", `test:${Buffer.alloc(32, 7).toString("base64")}`);
const NOW = Date.UTC(2026, 10, 4, 12);

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

interface FakeSite {
  cloudId: string;
  url: string;
  name: string;
  scopes?: string[];
  projects?: { id: string; key: string; name: string }[];
}

/** Atlassian as far as Connect Jira uses it. Tokens are `access-<n>` / `refresh-<n>`, rotating on each refresh. */
function fakeAtlassian(sites: FakeSite[], over: { webhookErrors?: string[]; refreshError?: string; deleteStatus?: number } = {}) {
  const state = {
    issued: 0,
    refreshes: [] as string[],
    registered: [] as { cloudId: string; body: { url: string; webhooks: { events: string[]; jqlFilter: string }[] } }[],
    deleted: [] as { cloudId: string; ids: number[] }[],
    nextWebhook: 100,
    calls: [] as string[],
  };
  const live = new Set<string>();
  const issue = (scope = "read:jira-work read:jira-user manage:jira-webhook offline_access") => {
    state.issued++;
    live.add(`access-${state.issued}`);
    return json({ access_token: `access-${state.issued}`, refresh_token: `refresh-${state.issued}`, expires_in: 3600, scope, token_type: "Bearer" });
  };
  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    state.calls.push(`${method} ${url.pathname}`);
    if (url.origin === "https://auth.atlassian.com" && url.pathname === "/oauth/token") {
      const body = JSON.parse(String(init?.body));
      if (body.client_id !== CONFIG.clientId || body.client_secret !== CONFIG.clientSecret) return json({ error: "access_denied" }, 401);
      if (body.grant_type === "authorization_code") return body.code === "good-code" ? issue() : json({ error: "invalid_grant" }, 403);
      if (body.grant_type === "refresh_token") {
        state.refreshes.push(body.refresh_token);
        if (over.refreshError) return json({ error: over.refreshError }, 403);
        return issue();
      }
      return json({ error: "unsupported_grant_type" }, 400);
    }
    const auth = new Headers(init?.headers).get("authorization")?.replace("Bearer ", "") ?? "";
    if (!live.has(auth)) return json({ message: "Unauthorized" }, 401);
    if (url.pathname === "/oauth/token/accessible-resources") {
      return json(sites.map((s) => ({ id: s.cloudId, url: s.url, name: s.name, scopes: s.scopes ?? ["read:jira-work", "manage:jira-webhook"] })));
    }
    const match = url.pathname.match(/^\/ex\/jira\/([^/]+)\/rest\/api\/3\/(.+)$/);
    const site = sites.find((s) => s.cloudId === match?.[1]);
    if (!match || !site) return json({ errorMessages: ["Not found"] }, 404);
    const path = match[2];
    if (path === "project/search") {
      const all = site.projects ?? [];
      const startAt = Number(url.searchParams.get("startAt"));
      const max = Number(url.searchParams.get("maxResults"));
      const values = all.slice(startAt, startAt + max);
      return json({ values, isLast: startAt + values.length >= all.length, total: all.length });
    }
    if (path === "myself") return json({ accountId: "557058:integration", displayName: "Ember Integration" });
    if (path === "webhook" && method === "POST") {
      const body = JSON.parse(String(init?.body));
      state.registered.push({ cloudId: site.cloudId, body });
      if (over.webhookErrors) return json({ webhookRegistrationResult: [{ errors: over.webhookErrors }] });
      return json({ webhookRegistrationResult: [{ createdWebhookId: state.nextWebhook++ }] });
    }
    if (path === "webhook" && method === "DELETE") {
      if (over.deleteStatus) return json({ errorMessages: ["Forbidden"] }, over.deleteStatus);
      state.deleted.push({ cloudId: site.cloudId, ids: JSON.parse(String(init?.body)).webhookIds });
      return new Response(null, { status: 202 });
    }
    return json({ errorMessages: ["Not found"] }, 404);
  }) as typeof fetch;
  return { client: new AtlassianClient(CONFIG, fetchImpl), state };
}

const ACME: FakeSite = {
  cloudId: "cloud-acme",
  url: "https://acme.atlassian.net",
  name: "Acme",
  projects: [
    { id: "10000", key: "ENG", name: "Engineering" },
    { id: "10001", key: "OPS", name: "Operations" },
  ],
};

class MemoryStore implements VerificationStore {
  rows = new Map<string, { value: string; expiresAt: Date }>();
  async createVerificationValue({ identifier, value, expiresAt }: { identifier: string; value: string; expiresAt: Date }) {
    this.rows.set(identifier, { value, expiresAt });
    return {};
  }
  async findVerificationValue(identifier: string) {
    return this.rows.get(identifier) ?? null;
  }
  async deleteVerificationByIdentifier(identifier: string) {
    this.rows.delete(identifier);
  }
}

describe("Jira app settings", () => {
  test("Connect Jira is unavailable until the client ID, secret and an https intake address are set", () => {
    const full = { JIRA_OAUTH_CLIENT_ID: " id ", JIRA_OAUTH_CLIENT_SECRET: "secret", JIRA_WEBHOOK_URL: "https://ember.example/webhook/jira" };
    assert.deepEqual(jiraAppConfig(full), { clientId: "id", clientSecret: "secret", webhookUrl: "https://ember.example/webhook/jira" });
    for (const missing of ["JIRA_OAUTH_CLIENT_ID", "JIRA_OAUTH_CLIENT_SECRET", "JIRA_WEBHOOK_URL"]) {
      assert.equal(jiraAppConfig({ ...full, [missing]: "" }), null, missing);
    }
    assert.equal(jiraAppConfig({ ...full, JIRA_WEBHOOK_URL: "http://ember.example/webhook/jira" }), null, "Jira delivers only to https");
    assert.equal(jiraAppConfig({ ...full, JIRA_WEBHOOK_URL: "not a url" }), null);
  });

  test("consent asks for the scopes Connect Jira and enrich use, and always shows the consent screen", () => {
    const url = new URL(authorizeUrl(CONFIG, "st4te", "https://app.example/connect/jira/callback"));
    assert.equal(url.origin + url.pathname, "https://auth.atlassian.com/authorize");
    assert.equal(url.searchParams.get("audience"), "api.atlassian.com");
    assert.equal(url.searchParams.get("scope"), "read:jira-work read:jira-user manage:jira-webhook offline_access");
    assert.equal(url.searchParams.get("state"), "st4te");
    assert.equal(url.searchParams.get("prompt"), "consent");
    assert.equal(url.searchParams.get("response_type"), "code");
  });

  test("the state matches only the browser and user that started the round trip, and GitHub's cookies mean nothing here", () => {
    const sealed = seal({ state: "abc", organizationId: "org-1", userId: "u-1" }, SECRET, 60);
    assert.equal(matchState(sealed, "abc", "u-1", SECRET)?.organizationId, "org-1");
    assert.equal(matchState(sealed, "abd", "u-1", SECRET), null);
    assert.equal(matchState(sealed, "abc", "u-2", SECRET), null);
    const github = sealGitHub({ state: "abc", organizationId: "org-1", userId: "u-1", mode: "install" }, SECRET, 60);
    assert.equal(unseal(github, SECRET), null, "each flow signs with its own key");
  });

  test("the webhook's JQL names projects by id", () => {
    assert.equal(projectJql(["10000", "10001"]), "project in (10000, 10001)");
    assert.throws(() => projectJql([]));
    assert.throws(() => projectJql(["ENG"]), "a key changes on rename, and it isn't quoted");
    assert.throws(() => projectJql(["1) or project != (2"]));
  });
});

describe("Atlassian's API", () => {
  test("the code becomes a token pair, and a refused code or a missing refresh token fails", async () => {
    const { client } = fakeAtlassian([ACME]);
    const tokens = await client.exchangeCode("good-code", "https://app.example/cb", NOW);
    assert.deepEqual(tokens, {
      accessToken: "access-1",
      refreshToken: "refresh-1",
      expiresAt: NOW / 1000 + 3600,
      scope: "read:jira-work read:jira-user manage:jira-webhook offline_access",
    });
    await assert.rejects(client.exchangeCode("bad-code", "https://app.example/cb"), (e: AtlassianError) => e.code === "invalid_grant");
    const noRefresh = new AtlassianClient(CONFIG, (async () => json({ access_token: "a", expires_in: 3600 })) as typeof fetch);
    await assert.rejects(noRefresh.exchangeCode("good-code", "x"), /offline_access/);
  });

  test("only Jira sites with an https URL are offered, by their host", async () => {
    const { client } = fakeAtlassian([
      ACME,
      { cloudId: "cloud-wiki", url: "https://acme.atlassian.net/wiki", name: "Wiki", scopes: ["read:confluence-content.all"] },
      { cloudId: "cloud-odd", url: "javascript:alert(1)", name: "Odd" },
      { cloudId: "cloud-beta", url: "https://Beta.Atlassian.net", name: "" },
    ]);
    const tokens = await client.exchangeCode("good-code", "x");
    assert.deepEqual(await client.sites(tokens.accessToken), [
      { cloudId: "cloud-acme", host: "acme.atlassian.net", name: "Acme" },
      { cloudId: "cloud-beta", host: "beta.atlassian.net", name: "beta.atlassian.net" },
    ]);
  });

  test("projects are read page by page, up to the picker's limit", async () => {
    const many = Array.from({ length: MAX_PROJECTS + 30 }, (_, i) => ({ id: String(20000 + i), key: `P${i}`, name: `Project ${i}` }));
    const { client, state } = fakeAtlassian([ACME, { ...ACME, cloudId: "cloud-big", projects: many }]);
    const tokens = await client.exchangeCode("good-code", "x");
    assert.deepEqual(await client.projects(tokens.accessToken, "cloud-acme"), { projects: ACME.projects, more: false });
    const big = await client.projects(tokens.accessToken, "cloud-big");
    assert.equal(big.projects.length, MAX_PROJECTS);
    assert.equal(big.more, true);
    assert.equal(state.calls.filter((c) => c.endsWith("cloud-big/rest/api/3/project/search")).length, MAX_PROJECTS / 50);
  });

  test("a webhook Jira won't register fails with its reason", async () => {
    const { client } = fakeAtlassian([ACME], { webhookErrors: ["Webhook limit reached"] });
    const tokens = await client.exchangeCode("good-code", "x");
    await assert.rejects(client.registerWebhook(tokens.accessToken, "cloud-acme", "project in (10000)"), /Webhook limit reached/);
  });
});

describe("a consent waiting for the picker", () => {
  const consent: PendingConsent = {
    organizationId: "org-1",
    userId: "u-1",
    tokens: { accessToken: "access-1", refreshToken: "refresh-1", expiresAt: NOW / 1000 + 3600, scope: "" },
    atlassian: { accountId: "557058:integration", displayName: "Ember Integration" },
    sites: [],
  };

  test("is kept sealed server side and comes back only to the same organization and user, until it expires", async () => {
    const store = new MemoryStore();
    const pendingId = await savePending(store, RING, consent, NOW);
    const [stored] = [...store.rows.values()];
    assert.ok(!stored.value.includes("access-1") && !stored.value.includes("refresh-1"), "the tokens are sealed");
    const pick = { pendingId, organizationId: "org-1", userId: "u-1" };
    assert.deepEqual(await loadPending(store, RING, pick, NOW + 1000), consent);
    assert.equal(await loadPending(store, RING, { ...pick, userId: "u-2" }, NOW), null);
    assert.equal(await loadPending(store, RING, { ...pick, organizationId: "org-2" }, NOW), null);
    assert.equal(await loadPending(store, RING, pick, NOW + 15 * 60 * 1000), null, "expired");
    assert.equal(await loadPending(store, RING, { ...pick, pendingId: "../other" }, NOW), null);
    await deletePending(store, pendingId);
    assert.equal(await loadPending(store, RING, pick, NOW), null);
  });

  test("a sealed consent can't be passed off as another one", async () => {
    const store = new MemoryStore();
    const one = await savePending(store, RING, consent, NOW);
    const two = await savePending(store, RING, consent, NOW);
    store.rows.set(`connect-jira:${two}`, store.rows.get(`connect-jira:${one}`)!);
    assert.equal(await loadPending(store, RING, { pendingId: two, organizationId: "org-1", userId: "u-1" }, NOW), null);
  });
});

describe("a stored Jira credential", () => {
  test("is accepted only in the shape Connect Jira writes", () => {
    const good = { access_token: "a", refresh_token: "r", expires_at: 1, cloud_id: "c", webhook_ids: [1], scope: "s", jql: "j" };
    assert.equal(asJiraCredential(good)?.atlassian_name, "");
    assert.equal(asJiraCredential({ ...good, webhook_ids: ["1"] }), null);
    assert.equal(asJiraCredential({ ...good, expires_at: "1" }), null);
    assert.equal(asJiraCredential({ api_token: "legacy" }), null);
    assert.equal(asJiraCredential(null), null);
  });
});

// ---------------------------------------------------------------------------

const postgresUrl = process.env.TEST_POSTGRES_URL;

describe("Connect Jira on Postgres", { skip: postgresUrl ? false : "set TEST_POSTGRES_URL to run it" }, () => {
  let pool: pg.Pool;
  let ready = false;
  // Postgres keeps rows between runs, so hosts and tenants are unique to this run
  const run = Date.now().toString(36);
  const tenant = (name: string) => `${name}-${run}`;
  let n = 0;

  before(async () => {
    pool = new pg.Pool({ connectionString: postgresUrl });
    const { rows } = await pool.query<{ ready: boolean }>("select to_regclass('public.source_credentials') is not null as ready");
    ready = rows[0]?.ready ?? false;
  });
  after(async () => {
    await pool?.end();
  });

  /** A site of its own for each test, and a consent for it as the callback would leave it. */
  async function setup(over: Parameters<typeof fakeAtlassian>[1] = {}) {
    n++;
    const site: FakeSite = { ...ACME, cloudId: `cloud-${run}-${n}`, url: `https://acme-${run}-${n}.atlassian.net` };
    const fake = fakeAtlassian([site], over);
    const tokens = await fake.client.exchangeCode("good-code", "x");
    const [found] = await fake.client.sites(tokens.accessToken);
    const pendingSite: PendingSite = { ...found, projects: site.projects!, more: false, blocked: null };
    const consent: PendingConsent = {
      organizationId: "org-1",
      userId: "u-1",
      tokens,
      atlassian: { accountId: "557058:integration", displayName: "Ember Integration" },
      sites: [pendingSite],
    };
    return { ...fake, site: pendingSite, consent, host: found.host };
  }

  async function installation(host: string) {
    const { rows } = await pool.query("select tenant, label, external_id, connected_by, disconnected_at from public.installations where source = 'jira' and account_id = $1", [host]);
    return rows[0];
  }

  test("connecting registers a webhook for the ticked projects, maps the site and seals the tokens", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { client, state, site, consent, host } = await setup();
    const result = await connectSite(pool, RING, client, { consent, site, projectIds: ["10001", "99999"], tenant: tenant("acme"), userId: "u-1" });
    assert.deepEqual(result, { ok: true, host, reconnected: false });

    assert.equal(state.registered.length, 1);
    assert.deepEqual(state.registered[0].body, {
      url: CONFIG.webhookUrl,
      webhooks: [{ events: [...WEBHOOK_EVENTS], jqlFilter: "project in (10001)" }],
    }, "a project the site doesn't have is dropped");
    assert.deepEqual(await installation(host), { tenant: tenant("acme"), label: host, external_id: site.cloudId, connected_by: "u-1", disconnected_at: null });
    const credential = asJiraCredential(await loadCredential(pool, RING, { source: "jira", accountId: host }));
    assert.deepEqual(credential, {
      access_token: "access-1",
      refresh_token: "refresh-1",
      expires_at: consent.tokens.expiresAt,
      scope: consent.tokens.scope,
      cloud_id: site.cloudId,
      webhook_ids: [100],
      jql: "project in (10001)",
      atlassian_account_id: "557058:integration",
      atlassian_name: "Ember Integration",
    });
  });

  test("no ticked project registers nothing", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { client, state, site, consent, host } = await setup();
    assert.deepEqual(await connectSite(pool, RING, client, { consent, site, projectIds: [], tenant: tenant("acme"), userId: "u-1" }), {
      ok: false,
      reason: "no-projects",
    });
    assert.equal(state.registered.length, 0);
    assert.equal(await installation(host), undefined);
  });

  test("reconnecting replaces the token pair and deletes the earlier webhook", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { client, state, site, consent, host } = await setup();
    const input = { consent, site, projectIds: ["10000"], tenant: tenant("acme"), userId: "u-1" };
    await connectSite(pool, RING, client, input);
    const again = { ...consent, tokens: await client.exchangeCode("good-code", "x") };
    assert.deepEqual(await connectSite(pool, RING, client, { ...input, consent: again, projectIds: ["10000", "10001"] }), { ok: true, host, reconnected: true });
    assert.deepEqual(state.deleted, [{ cloudId: site.cloudId, ids: [100] }]);
    const credential = asJiraCredential(await loadCredential(pool, RING, { source: "jira", accountId: host }));
    assert.deepEqual([credential?.access_token, credential?.webhook_ids, credential?.jql], ["access-2", [101], "project in (10000, 10001)"]);
  });

  test("a site connected to another tenant isn't moved, and the webhook just registered is deleted", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { client, state, site, consent, host } = await setup();
    await connectSite(pool, RING, client, { consent, site, projectIds: ["10000"], tenant: tenant("acme"), userId: "u-1" });
    const before = await loadCredential(pool, RING, { source: "jira", accountId: host });
    const result = await connectSite(pool, RING, client, { consent, site, projectIds: ["10000"], tenant: tenant("other"), userId: "u-9" });
    assert.deepEqual(result, { ok: false, reason: "other-tenant" });
    assert.deepEqual(state.deleted, [{ cloudId: site.cloudId, ids: [101] }], "only the new webhook");
    assert.equal((await installation(host)).tenant, tenant("acme"));
    assert.deepEqual(await loadCredential(pool, RING, { source: "jira", accountId: host }), before, "acme's credential is untouched");
  });

  test("an expiring token is refreshed under the row lock and the rotated refresh token is stored", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { client, state, site, consent, host } = await setup();
    await connectSite(pool, RING, client, { consent, site, projectIds: ["10000"], tenant: tenant("acme"), userId: "u-1" });
    const fresh = () => freshJiraCredential(pool, RING, client, host, () => consent.tokens.expiresAt * 1000 - 60_000);
    assert.equal((await fresh())?.access_token, "access-2", "within five minutes of expiry");
    assert.deepEqual(state.refreshes, ["refresh-1"]);
    const stored = asJiraCredential(await loadCredential(pool, RING, { source: "jira", accountId: host }));
    assert.deepEqual([stored?.access_token, stored?.refresh_token, stored?.webhook_ids], ["access-2", "refresh-2", [100]]);
    // Two at once: the second waits for the first's commit and finds a token that is still good
    const [one, two] = await Promise.all([
      freshJiraCredential(pool, RING, client, host, () => stored!.expires_at * 1000),
      freshJiraCredential(pool, RING, client, host, () => stored!.expires_at * 1000),
    ]);
    assert.deepEqual(state.refreshes, ["refresh-1", "refresh-2"], "refreshed once");
    assert.equal(one?.refresh_token, two?.refresh_token);
    assert.equal(await freshJiraCredential(pool, RING, client, "nobody.atlassian.net"), null);
  });

  test("Disconnect deletes the webhook with the stored token, and reports when it can't", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const { client, state, site, consent, host } = await setup();
    await connectSite(pool, RING, client, { consent, site, projectIds: ["10000"], tenant: tenant("acme"), userId: "u-1" });
    assert.equal(await removeWebhooks(pool, RING, client, host), "deleted");
    assert.deepEqual(state.deleted, [{ cloudId: site.cloudId, ids: [100] }]);
    assert.equal(await removeWebhooks(pool, RING, client, "nobody.atlassian.net"), "none");

    const refused = await setup({ deleteStatus: 403 });
    await connectSite(pool, RING, refused.client, { consent: refused.consent, site: refused.site, projectIds: ["10000"], tenant: tenant("acme"), userId: "u-1" });
    assert.equal(await removeWebhooks(pool, RING, refused.client, refused.host), "failed");
  });
});
