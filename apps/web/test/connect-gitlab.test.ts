// EMBER-149 acceptance (ADR 0010, GitLab): an owner connects a top-level
// group with a pasted read_api token. The token's user must be a member of the
// group, a token that can write is refused, a personal access token is
// labelled as weaker proof, the group maps to `<instance host>/<group>` as
// intake derives it, and never moves to another tenant. Each webhook's
// signing token is pasted back and stored for intake. GitLab is a fake `fetch`
// here. The connection writes run on Postgres when TEST_POSTGRES_URL is set
// and the ingestion migrations are applied (dbmate up).
// Run: npm test            (no Postgres: those tests skip)
//      TEST_POSTGRES_URL=postgres://... npm test

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { after, before, describe, test } from "node:test";
import pg from "pg";
import { disconnectAccount } from "../src/lib/connections.ts";
import {
  addSigningToken,
  connectGroup,
  connectionLabel,
  GitLabClient,
  gitlabConnectConfig,
  MAX_SIGNING_TOKENS,
  parseGroup,
  parseInstance,
  parseSigningToken,
  signingTokenCount,
  verifyGroup,
} from "../src/lib/gitlab-connect.ts";
import { KeyRing, loadCredential } from "../src/lib/source-credentials.ts";

const API_RING = KeyRing.parse("api", `test:${Buffer.alloc(32, 7).toString("base64")}`);
const WEBHOOK_RING = KeyRing.parse("webhook", `test:${Buffer.alloc(32, 9).toString("base64")}`);
const CONFIG = { webhookUrl: "https://ember.example/webhook/gitlab" };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function signingToken(): string {
  return `whsec_${randomBytes(32).toString("base64")}`;
}

interface FakeGroup {
  id: number;
  path: string;
  name?: string;
  parentId?: number | null;
  /** user id → access level */
  members?: Record<number, number>;
}

/** GitLab's API as far as Connect GitLab uses it, for one token. */
function fakeGitLab(
  groups: FakeGroup[],
  over: { token?: string; userId?: number; username?: string; bot?: boolean; scopes?: string[]; active?: boolean; base?: string } = {},
) {
  const token = over.token ?? "glpat-good";
  const userId = over.userId ?? 11;
  const base = over.base ?? "https://gitlab.com/api/v4";
  const calls: string[] = [];
  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    calls.push(`${url.origin}${url.pathname}`);
    assert.equal(init?.redirect, "manual", "redirects are never followed");
    assert.ok(String(input).startsWith(`${base}/`), `the token only goes to ${base}: ${input}`);
    const headers = new Headers(init?.headers);
    if (headers.get("private-token") !== token) return json({ message: "401 Unauthorized" }, 401);
    const path = url.pathname.slice(new URL(base).pathname.length);
    if (path === "/personal_access_tokens/self") {
      return json({ id: 5, user_id: userId, scopes: over.scopes ?? ["read_api"], active: over.active ?? true, expires_at: "2027-11-01" });
    }
    if (path === "/user") return json({ id: userId, username: over.username ?? (over.bot ? "group_9_bot_abc" : "pat"), bot: over.bot ?? false });
    const groupMatch = /^\/groups\/([^/]+)$/.exec(path);
    if (groupMatch) {
      const wanted = decodeURIComponent(groupMatch[1]);
      const group = groups.find((g) => g.path.toLowerCase() === wanted.toLowerCase());
      return group
        ? json({ id: group.id, full_path: group.path, name: group.name ?? group.path, parent_id: group.parentId ?? null })
        : json({ message: "404 Group Not Found" }, 404);
    }
    const memberMatch = /^\/groups\/(\d+)\/members\/all\/(\d+)$/.exec(path);
    if (memberMatch) {
      const group = groups.find((g) => g.id === Number(memberMatch[1]));
      const level = group?.members?.[Number(memberMatch[2])];
      return level ? json({ id: Number(memberMatch[2]), access_level: level }) : json({ message: "404 Not found" }, 404);
    }
    throw new Error(`unexpected GitLab call ${path}`);
  }) as typeof fetch;
  return { fetchImpl, calls };
}

const ACME: FakeGroup = { id: 9, path: "Acme", name: "Acme Corp", members: { 11: 20 } };

describe("what an owner pastes", () => {
  test("the instance is a plain https URL; intake's host is lower case without the port", () => {
    assert.deepEqual(parseInstance(""), { baseUrl: "https://gitlab.com", host: "gitlab.com" });
    assert.deepEqual(parseInstance(" https://gitlab.com/ "), { baseUrl: "https://gitlab.com", host: "gitlab.com" });
    assert.deepEqual(parseInstance("https://GitLab.Example.com:8443/gitlab/"), {
      baseUrl: "https://gitlab.example.com:8443/gitlab",
      host: "gitlab.example.com",
    });
    for (const bad of ["http://gitlab.com", "https://user:pw@gitlab.com", "https://gitlab.com/?x=1", "https://gitlab.com/#x", "https://gitlab.com/api/v4", "gitlab.com", "ftp://gitlab.com"]) {
      assert.equal(parseInstance(bad), null, bad);
    }
  });

  test("the group is one top-level path", () => {
    assert.equal(parseGroup(" acme "), "acme");
    assert.equal(parseGroup("/gitlab-org/"), "gitlab-org");
    assert.equal(parseGroup("my.group_1"), "my.group_1");
    for (const bad of ["", "acme/platform", "a b", "-acme", "acme.git", "../x"]) assert.equal(parseGroup(bad), null, bad);
  });

  test("a signing token is whsec_ and a whole 32-byte key", () => {
    const token = signingToken();
    assert.equal(parseSigningToken(` ${token}\n`), token);
    assert.equal(parseSigningToken(token.slice(0, -4)), null, "cut short");
    assert.equal(parseSigningToken(token.replace("whsec_", "")), null);
    assert.equal(parseSigningToken(`whsec_${randomBytes(16).toString("base64")}`), null, "16 bytes");
    assert.equal(parseSigningToken(`${token}!`), null);
  });

  test("Connect GitLab needs intake's https address", () => {
    assert.deepEqual(gitlabConnectConfig({ GITLAB_WEBHOOK_URL: " https://ember.example/webhook/gitlab " }), CONFIG);
    assert.equal(gitlabConnectConfig({}), null);
    assert.equal(gitlabConnectConfig({ GITLAB_WEBHOOK_URL: "http://ember.example/webhook/gitlab" }), null);
  });
});

describe("reading the group is the proof", () => {
  const client = (fake: ReturnType<typeof fakeGitLab>, token = "glpat-good") => new GitLabClient(parseInstance("https://gitlab.com")!, token, fake.fetchImpl);

  test("a member's personal access token connects, labelled as the weaker proof", async () => {
    const result = await verifyGroup(client(fakeGitLab([ACME])), "acme");
    assert.ok(result.ok);
    assert.equal(result.value.tokenKind, "personal");
    assert.equal(result.value.group.fullPath, "Acme", "GitLab's own spelling, as path_with_namespace has it");
    assert.equal(connectionLabel(result.value.group, "personal", "pat"), "Acme Corp (personal access token of @pat)");
  });

  test("the group's own bot is a group access token", async () => {
    const result = await verifyGroup(client(fakeGitLab([ACME], { bot: true })), "Acme");
    assert.ok(result.ok);
    assert.equal(result.value.tokenKind, "group");
    assert.equal(connectionLabel(result.value.group, "group", "group_9_bot_abc"), "Acme Corp");
  });

  test("each way a token fails to prove the group is refused", async () => {
    const cases: [string, ReturnType<typeof fakeGitLab>, string, string?][] = [
      ["a wrong token", fakeGitLab([ACME]), "invalid-token", "glpat-wrong"],
      ["a revoked token", fakeGitLab([ACME], { active: false }), "invalid-token"],
      ["a token that writes", fakeGitLab([ACME], { scopes: ["api", "read_api"] }), "write-scope"],
      ["a token that pushes", fakeGitLab([ACME], { scopes: ["read_api", "write_repository"] }), "write-scope"],
      ["a token without read_api", fakeGitLab([ACME], { scopes: ["read_user"] }), "scope"],
      ["a group it can't see", fakeGitLab([]), "not-found"],
      ["a subgroup", fakeGitLab([{ ...ACME, parentId: 1 }]), "not-top-level"],
      // Anyone can read a public group
      ["a public group it isn't a member of", fakeGitLab([{ ...ACME, members: {} }]), "not-member"],
    ];
    for (const [name, fake, reason, token] of cases) {
      assert.deepEqual(await verifyGroup(client(fake, token), "acme"), { ok: false, reason }, name);
    }
  });
});

const postgresUrl = process.env.TEST_POSTGRES_URL;

describe("connecting on Postgres", { skip: postgresUrl ? false : "set TEST_POSTGRES_URL to run it" }, () => {
  let pool: pg.Pool;
  let ready = false;
  // Postgres keeps rows between runs, so groups and tenants are unique to this run
  const run = Date.now().toString(36);
  let groups = 0;
  const group = (name: string): FakeGroup => ({ ...ACME, id: (Date.now() % 1_000_000) * 100 + ++groups, path: `${name}-${run}`, name });
  const tenant = (name: string) => `${name}-${run}`;

  before(async () => {
    pool = new pg.Pool({ connectionString: postgresUrl });
    const { rows } = await pool.query<{ ready: boolean }>("select to_regclass('public.source_credentials') is not null as ready");
    ready = rows[0]?.ready ?? false;
  });
  after(async () => {
    await pool?.end();
  });

  function connect(g: FakeGroup, over: { tenant?: string; token?: string; bot?: boolean; instance?: string; base?: string } = {}) {
    const fake = fakeGitLab([g], { token: over.token, bot: over.bot, base: over.base });
    const result = connectGroup(
      pool,
      API_RING,
      CONFIG,
      { instance: over.instance ?? "https://gitlab.com", group: g.path.toLowerCase(), token: over.token ?? "glpat-good", tenant: over.tenant ?? tenant("acme"), userId: "u-1" },
      fake.fetchImpl,
    );
    return result;
  }

  async function installation(accountId: string) {
    const { rows } = await pool.query("select tenant, label, external_id, connected_by, disconnected_at from public.installations where source = 'gitlab' and account_id = $1", [
      accountId,
    ]);
    return rows[0];
  }

  test("a connected group is mapped as intake derives it, its token stored for enrich, and waiting deliveries handed over", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const g = group("acme");
    const accountId = `gitlab.com/${g.path}`;
    await pool.query(
      `insert into public.raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)
       values ('gitlab', $1, $2, null, 'push', 'webhook', '{}')`,
      [`waiting-${run}`, accountId],
    );
    const result = await connect(g);
    assert.ok(result.ok);
    assert.deepEqual(
      { accountId: result.accountId, tokenKind: result.tokenKind, webhookUrl: result.webhookUrl, reconnected: result.reconnected },
      { accountId, tokenKind: "personal", webhookUrl: CONFIG.webhookUrl, reconnected: false },
    );
    assert.deepEqual(await installation(accountId), {
      tenant: tenant("acme"),
      label: "acme (personal access token of @pat)",
      external_id: `gitlab.com:${g.id}`,
      connected_by: "u-1",
      disconnected_at: null,
    });
    // What enrich:gitlab reads (services/gitlab-ingestion/enrich.py)
    const credential = await loadCredential(pool, API_RING, { source: "gitlab", accountId });
    assert.equal(credential?.token, "glpat-good");
    assert.equal(credential?.base_url, "https://gitlab.com");
    assert.equal(credential?.token_kind, "personal");
    assert.deepEqual(credential?.scopes, ["read_api"]);
    // No webhook yet: intake refuses the group's deliveries until one's token is pasted
    assert.equal(await loadCredential(pool, WEBHOOK_RING, { source: "gitlab", accountId }), null);
    assert.equal(await signingTokenCount(pool, WEBHOOK_RING, accountId, tenant("acme")), 0);
    const { rows } = await pool.query("select tenant from public.raw_events where delivery_id = $1", [`waiting-${run}`]);
    assert.equal(rows[0].tenant, tenant("acme"));
  });

  test("a self-managed group's token is stored with its own instance", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const g = group("selfhosted");
    const result = await connect(g, { instance: "https://GitLab.Example.com:8443/gitlab", base: "https://gitlab.example.com:8443/gitlab/api/v4", bot: true });
    assert.ok(result.ok);
    assert.equal(result.accountId, `gitlab.example.com/${g.path}`);
    assert.equal(result.label, "selfhosted");
    const credential = await loadCredential(pool, API_RING, { source: "gitlab", accountId: result.accountId });
    assert.equal(credential?.base_url, "https://gitlab.example.com:8443/gitlab");
  });

  test("a group never moves to another tenant, renamed or not", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const g = group("globex");
    assert.ok((await connect(g)).ok);
    assert.deepEqual(await connect(g, { tenant: tenant("initech") }), { ok: false, reason: "other-tenant" });
    // Renamed in GitLab: a new path, the same group id
    assert.deepEqual(await connect({ ...g, path: `${g.path}-renamed` }, { tenant: tenant("initech") }), { ok: false, reason: "other-tenant" });
    assert.equal((await installation(`gitlab.com/${g.path}`)).tenant, tenant("acme"));
  });

  test("each webhook's signing token is added for intake, once, only to this tenant's connected group", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const g = group("hooks");
    const accountId = `gitlab.com/${g.path}`;
    assert.ok((await connect(g)).ok);
    const first = signingToken();
    const second = signingToken();
    const add = (token: string, over: { tenant?: string; accountId?: string } = {}) =>
      addSigningToken(pool, WEBHOOK_RING, { accountId: over.accountId ?? accountId, tenant: over.tenant ?? tenant("acme"), token });
    assert.deepEqual(await add(first), { ok: true, count: 1, already: false });
    assert.deepEqual(await add(second), { ok: true, count: 2, already: false });
    assert.deepEqual(await add(` ${first} `), { ok: true, count: 2, already: true });
    assert.deepEqual(await add("whsec_short"), { ok: false, reason: "invalid-signing-token" });
    assert.deepEqual(await add(signingToken(), { tenant: tenant("initech") }), { ok: false, reason: "not-found" });
    assert.deepEqual(await add(signingToken(), { accountId: `gitlab.com/nobody-${run}` }), { ok: false, reason: "not-found" });
    // What intake opens (services/intake/intake.py, StoredSigningTokens)
    assert.deepEqual(await loadCredential(pool, WEBHOOK_RING, { source: "gitlab", accountId }), { signing_tokens: [first, second] });

    // Reconnecting with a new access token keeps the webhooks
    assert.ok((await connect(g, { token: "glpat-rotated" })).ok);
    assert.equal((await loadCredential(pool, API_RING, { source: "gitlab", accountId }))?.token, "glpat-rotated");
    assert.equal(await signingTokenCount(pool, WEBHOOK_RING, accountId, tenant("acme")), 2);

    // Disconnect deletes both, so intake refuses the webhooks and enrich has no token
    assert.deepEqual(await disconnectAccount(pool, { source: "gitlab", accountId, tenant: tenant("acme") }), { ok: true, already: false });
    assert.equal(await loadCredential(pool, WEBHOOK_RING, { source: "gitlab", accountId }), null);
    assert.equal(await loadCredential(pool, API_RING, { source: "gitlab", accountId }), null);
    assert.deepEqual(await add(signingToken()), { ok: false, reason: "not-found" });
    assert.equal(await signingTokenCount(pool, WEBHOOK_RING, accountId, tenant("acme")), null);
  });

  test("a group keeps at most MAX_SIGNING_TOKENS", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const g = group("many");
    const accountId = `gitlab.com/${g.path}`;
    assert.ok((await connect(g)).ok);
    const tokens = Array.from({ length: MAX_SIGNING_TOKENS }, signingToken);
    await pool.query(
      "insert into public.source_credentials (source, account_id, kind, key_id, ciphertext) values ('gitlab', $1, 'webhook', $2, $3)",
      [accountId, ...Object.values(WEBHOOK_RING.seal({ source: "gitlab", accountId }, { signing_tokens: tokens }))],
    );
    assert.deepEqual(await addSigningToken(pool, WEBHOOK_RING, { accountId, tenant: tenant("acme"), token: signingToken() }), { ok: false, reason: "limit" });
  });
});
