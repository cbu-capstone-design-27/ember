// EMBER-130 acceptance (ADR 0010): an owner connects GitHub from the web app,
// an installation GitHub doesn't list for the user is refused, and an account
// never moves to another tenant, a reinstall included. GitHub is a fake
// `fetch` here. The connection writes run on Postgres when TEST_POSTGRES_URL
// is set and the ingestion migrations are applied (dbmate up).
// Run: npm test            (no Postgres: those tests skip)
//      TEST_POSTGRES_URL=postgres://... npm test

import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";
import pg from "pg";
import { connectAccount, handOff, heldAccounts, listConnections, type ConnectInput } from "../src/lib/connections.ts";
import {
  authorizeUrl,
  candidateInstallations,
  GitHubClient,
  githubAppConfig,
  installUrl,
  matchState,
  seal,
  unseal,
  verifyInstallation,
  withUserToken,
  type GitHubAppConfig,
} from "../src/lib/github-connect.ts";

const SECRET = "test-secret-at-least-32-characters-long!!";
const CONFIG: GitHubAppConfig = { slug: "ember-ingest", clientId: "Iv1.client", clientSecret: "shh" };

describe("signed connect cookies", () => {
  test("a sealed value comes back until it expires", () => {
    const now = Date.UTC(2026, 10, 4, 12);
    const sealed = seal({ organizationId: "org-1" }, SECRET, 60, now);
    assert.equal(unseal<{ organizationId: string }>(sealed, SECRET, now + 59_000)?.organizationId, "org-1");
    assert.equal(unseal(sealed, SECRET, now + 60_000), null);
  });

  test("an altered value, or one signed with another secret, is refused", () => {
    const sealed = seal({ organizationId: "org-1" }, SECRET, 60);
    const [, signature] = sealed.split(".");
    const forged = `${Buffer.from(JSON.stringify({ organizationId: "org-2", exp: 9e9 })).toString("base64url")}.${signature}`;
    assert.equal(unseal(forged, SECRET), null);
    assert.equal(unseal(sealed, "another-secret-at-least-32-characters!!"), null);
    assert.equal(unseal("garbage", SECRET), null);
    assert.equal(unseal(undefined, SECRET), null);
  });

  test("the state matches only the browser and user that started the round trip", () => {
    const sealed = seal({ state: "abc", organizationId: "org-1", userId: "u-1", mode: "install" }, SECRET, 60);
    assert.equal(matchState(sealed, "abc", "u-1", SECRET)?.organizationId, "org-1");
    assert.equal(matchState(sealed, "abd", "u-1", SECRET), null, "GitHub echoed another state");
    assert.equal(matchState(sealed, null, "u-1", SECRET), null, "an install started on GitHub's page has no state");
    assert.equal(matchState(sealed, "abc", "u-2", SECRET), null, "another user signed in meanwhile");
    assert.equal(matchState(undefined, "abc", "u-1", SECRET), null, "another browser has no cookie");
  });
});

describe("GitHub App settings", () => {
  test("Connect GitHub is unavailable until the client ID and secret are set", () => {
    assert.equal(githubAppConfig({}), null);
    assert.equal(githubAppConfig({ GITHUB_APP_CLIENT_ID: "x" }), null);
    assert.deepEqual(githubAppConfig({ GITHUB_APP_CLIENT_ID: " x ", GITHUB_APP_CLIENT_SECRET: "y" }), {
      slug: "ember-ingest",
      clientId: "x",
      clientSecret: "y",
    });
  });

  test("the install and sign-in links carry the state", () => {
    assert.equal(installUrl(CONFIG, "s/1"), "https://github.com/apps/ember-ingest/installations/new?state=s%2F1");
    const url = new URL(authorizeUrl(CONFIG, "s1", "https://web.example/connect/github/callback"));
    assert.equal(url.origin + url.pathname, "https://github.com/login/oauth/authorize");
    assert.equal(url.searchParams.get("client_id"), "Iv1.client");
    assert.equal(url.searchParams.get("state"), "s1");
    assert.equal(url.searchParams.get("redirect_uri"), "https://web.example/connect/github/callback");
  });
});

type Account = { id: number; login: string; type: string };

/** A GitHub that knows one user, their installations and their organization roles. */
function fakeGitHub({
  user = { id: 1, login: "octo", type: "User" } as Account,
  installations = [] as { id: number; account: Account }[],
  roles = {} as Record<string, "admin" | "member" | 403 | 404>,
  code = "good-code",
}) {
  const calls: string[] = [];
  const revoked: string[] = [];
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    calls.push(`${method} ${url.pathname}${url.search}`);
    if (url.pathname === "/login/oauth/access_token") {
      const body = JSON.parse(String(init?.body));
      return body.code === code && body.client_secret === CONFIG.clientSecret
        ? json({ access_token: "user-token", token_type: "bearer" })
        : json({ error: "bad_verification_code" });
    }
    if (url.pathname === `/applications/${CONFIG.clientId}/token` && method === "DELETE") {
      revoked.push(JSON.parse(String(init?.body)).access_token);
      return new Response(null, { status: 204 });
    }
    const auth = new Headers(init?.headers).get("authorization");
    if (auth !== "Bearer user-token") return json({ message: "Bad credentials" }, 401);
    if (url.pathname === "/user") return json(user);
    if (url.pathname === "/user/installations") {
      const perPage = Number(url.searchParams.get("per_page"));
      const page = Number(url.searchParams.get("page"));
      return json({ total_count: installations.length, installations: installations.slice((page - 1) * perPage, page * perPage) });
    }
    const org = url.pathname.match(/^\/user\/memberships\/orgs\/(.+)$/)?.[1];
    if (org) {
      const role = roles[decodeURIComponent(org)] ?? 404;
      if (role === 404) return json({ message: "Not Found" }, 404);
      if (role === 403) return json({ message: "Resource not accessible by integration" }, 403);
      return json({ state: "active", role });
    }
    return json({ message: "Not Found" }, 404);
  }) as typeof fetch;
  return { client: new GitHubClient(CONFIG, fetchImpl), calls, revoked };
}

const acme = { id: 9001, login: "acme", type: "Organization" };
const octo = { id: 1, login: "octo", type: "User" };

describe("verifying an installation with the user's token", () => {
  test("an installation GitHub lists for the user is accepted, and the token is revoked", async () => {
    const gh = fakeGitHub({ installations: [{ id: 55, account: acme }] });
    const found = await withUserToken(gh.client, "good-code", "https://web/cb", (token) => verifyInstallation(gh.client, token, "55"));
    assert.equal(found?.account.login, "acme");
    assert.deepEqual(gh.revoked, ["user-token"]);
  });

  test("an installation the user can't see is refused: the id on the redirect proves nothing", async () => {
    const gh = fakeGitHub({ installations: [{ id: 55, account: acme }] });
    const found = await withUserToken(gh.client, "good-code", "https://web/cb", (token) => verifyInstallation(gh.client, token, "56"));
    assert.equal(found, null);
    const odd = await withUserToken(gh.client, "good-code", "https://web/cb", (token) => verifyInstallation(gh.client, token, "55 or 1=1"));
    assert.equal(odd, null);
  });

  test("every page of installations is read", async () => {
    const many = Array.from({ length: 101 }, (_, i) => ({ id: 1000 + i, account: { ...acme, id: 5000 + i, login: `org-${i}` } }));
    const gh = fakeGitHub({ installations: many });
    const found = await withUserToken(gh.client, "good-code", "https://web/cb", (token) => verifyInstallation(gh.client, token, "1100"));
    assert.equal(found?.account.login, "org-100");
    assert.equal(gh.calls.filter((c) => c.startsWith("GET /user/installations")).length, 2);
  });

  test("a code GitHub refuses fails before any lookup, and nothing is left to revoke", async () => {
    const gh = fakeGitHub({ installations: [{ id: 55, account: acme }] });
    await assert.rejects(withUserToken(gh.client, "stolen-code", "https://web/cb", () => Promise.resolve(1)), /bad_verification_code/);
    assert.deepEqual(gh.revoked, []);
  });

  test("the token is revoked when the check itself fails", async () => {
    const gh = fakeGitHub({});
    await assert.rejects(withUserToken(gh.client, "good-code", "https://web/cb", () => Promise.reject(new Error("boom"))), /boom/);
    assert.deepEqual(gh.revoked, ["user-token"]);
  });
});

describe("existing installations", () => {
  test("only installations the user administers can be picked", async () => {
    const gh = fakeGitHub({
      user: octo,
      installations: [
        { id: 1, account: octo }, // their own account
        { id: 2, account: { id: 2, login: "someone", type: "User" } }, // a collaborator's account
        { id: 3, account: acme }, // an organization they own
        { id: 4, account: { id: 9002, login: "globex", type: "Organization" } }, // one they only belong to
        { id: 5, account: { id: 9003, login: "initech", type: "Organization" } }, // the App can't read members
      ],
      roles: { acme: "admin", globex: "member", initech: 403 },
    });
    const candidates = await withUserToken(gh.client, "good-code", "https://web/cb", (token) => candidateInstallations(gh.client, token));
    const byId = Object.fromEntries(candidates.map((c) => [c.installationId, c]));
    assert.equal(byId["1"].blocked, null);
    assert.match(byId["2"].blocked ?? "", /Only someone/);
    assert.equal(byId["3"].blocked, null);
    assert.equal(byId["3"].accountId, "9001");
    assert.match(byId["4"].blocked ?? "", /owner of globex/);
    assert.match(byId["5"].blocked ?? "", /Members \(read\)/);
  });
});

// ---------------------------------------------------------------------------

const postgresUrl = process.env.TEST_POSTGRES_URL;

describe("connections on Postgres", { skip: postgresUrl ? false : "set TEST_POSTGRES_URL to run it" }, () => {
  let pool: pg.Pool;
  let ready = false;
  // Postgres keeps rows between runs, so ids are unique to this run
  const run = Date.now();
  const id = (n: number) => String(run * 10 + n);
  const tenant = (name: string) => `${name}-${run.toString(36)}`;

  before(async () => {
    pool = new pg.Pool({ connectionString: postgresUrl });
    const { rows } = await pool.query<{ ready: boolean }>(
      "select exists (select from information_schema.columns where table_schema = 'public' and table_name = 'installations' and column_name = 'external_id') as ready",
    );
    ready = rows[0]?.ready ?? false;
  });
  after(async () => {
    await pool?.end();
  });

  function input(over: Partial<ConnectInput>): ConnectInput {
    return { source: "github", accountId: id(0), tenant: tenant("acme"), label: "acme", externalId: id(9), userId: "u-1", ...over };
  }

  async function storeDelivery(accountId: string, delivery: string, tenantValue: string | null = null) {
    await pool.query(
      `insert into public.raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)
       values ('github', $1, $2, $3, 'issues:opened', 'webhook', '{}')`,
      [`${delivery}-${run}`, accountId, tenantValue],
    );
  }

  async function tenantOf(delivery: string) {
    const { rows } = await pool.query<{ tenant: string | null }>("select tenant from public.raw_events where delivery_id = $1", [`${delivery}-${run}`]);
    return rows[0]?.tenant;
  }

  test("connecting maps the installation, hands its stored deliveries to enrich and wakes the workers", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const listener = await pool.connect();
    const woke: string[] = [];
    listener.on("notification", (n) => woke.push(n.payload ?? ""));
    await listener.query("listen raw_events");
    try {
      await storeDelivery(id(1), "before-connect");
      await storeDelivery(id(2), "someone-elses");
      const result = await connectAccount(pool, input({ accountId: id(1), externalId: id(91), label: "acme" }));
      assert.deepEqual(result, { ok: true, reconnected: false });
      assert.equal(await tenantOf("before-connect"), tenant("acme"));
      assert.equal(await tenantOf("someone-elses"), null);
      await new Promise((resolve) => setTimeout(resolve, 200));
      assert.ok(woke.includes("github"), "pg_notify('raw_events', 'github')");

      const { rows } = await pool.query(
        "select tenant, label, external_id, connected_by, disconnected_at from public.installations where source = 'github' and account_id = $1",
        [id(1)],
      );
      assert.deepEqual(rows[0], { tenant: tenant("acme"), label: "acme", external_id: id(91), connected_by: "u-1", disconnected_at: null });
    } finally {
      await listener.query("unlisten raw_events");
      listener.release();
    }
  });

  test("the second hand-off catches a delivery that landed without a tenant after the connect", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(3), externalId: id(93) }));
    await storeDelivery(id(3), "raced");
    assert.equal(await handOff(pool, { source: "github", accountId: id(3), tenant: tenant("acme") }), 1);
    assert.equal(await tenantOf("raced"), tenant("acme"));
  });

  test("an installation another tenant holds is refused, and nothing is handed over", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(4), externalId: id(94), tenant: tenant("acme") }));
    await storeDelivery(id(4), "stays-unmapped");
    await pool.query("update public.raw_events set tenant = null where delivery_id = $1", [`stays-unmapped-${run}`]);
    const result = await connectAccount(pool, input({ accountId: id(4), externalId: id(94), tenant: tenant("globex") }));
    assert.deepEqual(result, { ok: false, reason: "other-tenant" });
    assert.equal(await tenantOf("stays-unmapped"), null);
    const { rows } = await pool.query("select tenant from public.installations where source = 'github' and account_id = $1", [id(4)]);
    assert.equal(rows[0].tenant, tenant("acme"));
  });

  test("a reinstall can't move the account to another tenant, but reconnects it to its own", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(5), externalId: id(95), tenant: tenant("acme") }));
    await pool.query("update public.installations set disconnected_at = now() where source = 'github' and account_id = $1", [id(5)]);
    // GitHub gives the reinstall a new installation id; the account id stays
    const moved = await connectAccount(pool, input({ accountId: id(6), externalId: id(95), tenant: tenant("globex") }));
    assert.deepEqual(moved, { ok: false, reason: "other-tenant" });
    const back = await connectAccount(pool, input({ accountId: id(6), externalId: id(95), tenant: tenant("acme") }));
    assert.deepEqual(back, { ok: true, reconnected: false });
  });

  test("connecting a disconnected installation again clears disconnected_at", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(7), externalId: id(97) }));
    await pool.query("update public.installations set disconnected_at = now() where source = 'github' and account_id = $1", [id(7)]);
    await storeDelivery(id(7), "while-disconnected");
    // A hand-off for a disconnected account hands nothing over
    assert.equal(await handOff(pool, { source: "github", accountId: id(7), tenant: tenant("acme") }), 0);
    const again = await connectAccount(pool, input({ accountId: id(7), externalId: id(97), label: "acme-renamed" }));
    assert.deepEqual(again, { ok: true, reconnected: true });
    assert.equal(await tenantOf("while-disconnected"), tenant("acme"));
    const connections = await listConnections(pool, tenant("acme"), "github");
    const row = connections.find((c) => c.accountId === id(7));
    assert.equal(row?.disconnectedAt, null);
    assert.equal(row?.label, "acme-renamed");
  });

  test("the picker learns which installations are taken without learning by whom", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(8), externalId: id(98), tenant: tenant("acme") }));
    const held = await heldAccounts(pool, "github", [
      { accountId: id(8), externalId: id(98) }, // connected
      { accountId: id(80), externalId: id(98) }, // a reinstall of the same account
      { accountId: id(81), externalId: id(99) }, // nobody's
    ]);
    assert.deepEqual(held.get(id(8)), { tenant: tenant("acme"), connected: true });
    assert.deepEqual(held.get(id(80)), { tenant: tenant("acme"), connected: false });
    assert.equal(held.has(id(81)), false);
  });
});
