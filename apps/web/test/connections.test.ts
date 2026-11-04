// EMBER-147 acceptance (ADR 0010): a source credential is sealed the same way
// the Python workers open it, and Disconnect stops ingesting an account while
// its data stays. Reconnecting the same account to the same tenant clears
// disconnected_at, and another tenant still can't take it. The Postgres tests
// run when TEST_POSTGRES_URL is set and the ingestion migrations are applied.
// Run: npm test            (no Postgres: those tests skip)
//      TEST_POSTGRES_URL=postgres://... npm test

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, describe, test } from "node:test";
import pg from "pg";
import { connectAccount, disconnectAccount, listConnections, type ConnectInput } from "../src/lib/connections.ts";
import { CredentialError, KeyRing, loadCredential, storeCredential, type CredentialKind } from "../src/lib/source-credentials.ts";

interface VectorCase {
  kind: CredentialKind;
  source: string;
  account_id: string;
  key_id: string;
  nonce: string;
  secret: Record<string, unknown>;
  ciphertext: string;
}
const VECTORS = JSON.parse(
  readFileSync(new URL("../../../packages/source-credentials/fixtures/vectors.json", import.meta.url), "utf8"),
) as { rings: Record<CredentialKind, string>; cases: VectorCase[] };
const KEY = Buffer.from(Array.from({ length: 32 }, (_, i) => i)).toString("base64");

function onlyKey(kind: CredentialKind, keyId: string): KeyRing {
  const entry = VECTORS.rings[kind].split(",").find((e) => e.trim().startsWith(`${keyId}:`));
  return KeyRing.parse(kind, entry!);
}

describe("source credentials match the Python workers", () => {
  test("every shared vector opens, and seals to the same bytes given its nonce", () => {
    for (const c of VECTORS.cases) {
      const ring = KeyRing.parse(c.kind, VECTORS.rings[c.kind]);
      const row = { source: c.source, accountId: c.account_id };
      const ciphertext = Buffer.from(c.ciphertext, "base64");
      assert.deepEqual(ring.open(row, c.key_id, ciphertext), c.secret, `${c.source} ${c.kind}`);
      const sealed = onlyKey(c.kind, c.key_id).seal(row, c.secret, Buffer.from(c.nonce, "base64"));
      assert.equal(sealed.keyId, c.key_id);
      assert.equal(sealed.ciphertext.toString("base64"), c.ciphertext, `${c.source} ${c.kind} seals to the vector`);
    }
  });

  test("a row moved to another account, source or kind doesn't open", () => {
    const c = VECTORS.cases[0];
    const ring = KeyRing.parse("api", VECTORS.rings.api);
    const ciphertext = Buffer.from(c.ciphertext, "base64");
    assert.throws(() => ring.open({ source: c.source, accountId: "evil.atlassian.net" }, c.key_id, ciphertext), CredentialError);
    assert.throws(() => ring.open({ source: "slack", accountId: c.account_id }, c.key_id, ciphertext), CredentialError);
    const sameKeyOtherKind = KeyRing.parse("webhook", `${c.key_id}:${VECTORS.rings.api.split(",")[0].split(":")[1]}`);
    assert.throws(() => sameKeyOtherKind.open({ source: c.source, accountId: c.account_id }, c.key_id, ciphertext), CredentialError);
  });

  test("a changed byte, an unknown key or a short row doesn't open", () => {
    const c = VECTORS.cases[0];
    const ring = KeyRing.parse("api", VECTORS.rings.api);
    const row = { source: c.source, accountId: c.account_id };
    const flipped = Buffer.from(c.ciphertext, "base64");
    flipped[flipped.length - 1] ^= 1;
    assert.throws(() => ring.open(row, c.key_id, flipped), CredentialError);
    assert.throws(() => ring.open(row, "retired", Buffer.from(c.ciphertext, "base64")), /rotated out/);
    assert.throws(() => ring.open(row, c.key_id, Buffer.from("short")), CredentialError);
  });

  test("the first key seals with a fresh nonce each time", () => {
    const ring = KeyRing.parse("api", VECTORS.rings.api);
    assert.equal(ring.current, "2026-11");
    assert.deepEqual(ring.keyIds, ["2026-11", "2026-10"]);
    const row = { source: "jira", accountId: "acme.atlassian.net" };
    const one = ring.seal(row, { access_token: "x" });
    const two = ring.seal(row, { access_token: "x" });
    assert.notEqual(one.ciphertext.toString("hex"), two.ciphertext.toString("hex"));
    assert.deepEqual(ring.open(row, one.keyId, one.ciphertext), { access_token: "x" });
  });

  test("each kind reads its own variable, and a bad ring is refused without echoing the key", () => {
    assert.equal(KeyRing.fromEnv("api", {}), null);
    assert.equal(KeyRing.fromEnv("api", { SOURCE_CREDENTIALS_WEBHOOK_KEYS: `w:${KEY}` }), null);
    assert.equal(KeyRing.fromEnv("webhook", { SOURCE_CREDENTIALS_WEBHOOK_KEYS: ` w:${KEY}\n` })?.current, "w");
    const bad: Record<string, string> = {
      "not base64": "k:not-base64!!",
      "short key": `k:${Buffer.alloc(16).toString("base64")}`,
      "no id": KEY,
      "bad id": `has space:${KEY}`,
      "repeated id": `k:${KEY},k:${KEY}`,
      empty: " , ",
    };
    for (const [name, value] of Object.entries(bad)) {
      assert.throws(
        () => KeyRing.parse("api", value),
        (error: unknown) => error instanceof CredentialError && !error.message.includes(KEY),
        name,
      );
    }
  });
});

// ---------------------------------------------------------------------------

const postgresUrl = process.env.TEST_POSTGRES_URL;

describe("disconnect on Postgres", { skip: postgresUrl ? false : "set TEST_POSTGRES_URL to run it" }, () => {
  let pool: pg.Pool;
  let ready = false;
  // Postgres keeps rows between runs, so ids are unique to this run
  const run = Date.now();
  const id = (n: number) => `${run}-${n}.atlassian.net`;
  const tenant = (name: string) => `${name}-${run.toString(36)}`;
  const ring = KeyRing.parse("api", VECTORS.rings.api);

  before(async () => {
    pool = new pg.Pool({ connectionString: postgresUrl });
    const { rows } = await pool.query<{ ready: boolean }>("select to_regclass('public.source_credentials') is not null as ready");
    ready = rows[0]?.ready ?? false;
  });
  after(async () => {
    await pool?.end();
  });

  function input(over: Partial<ConnectInput>): ConnectInput {
    return { source: "jira", accountId: id(0), tenant: tenant("acme"), label: "acme", externalId: null, userId: "u-1", ...over };
  }

  async function storeDelivery(accountId: string, delivery: string, tenantValue: string | null) {
    await pool.query(
      `insert into public.raw_events (source, delivery_id, account_id, tenant, event_name, origin, envelope)
       values ('jira', $1, $2, $3, 'jira:issue_updated', 'webhook', '{}')`,
      [`${delivery}-${run}`, accountId, tenantValue],
    );
  }

  async function connection(accountId: string) {
    const { rows } = await pool.query<{ tenant: string; disconnected_at: Date | null }>(
      "select tenant, disconnected_at from public.installations where source = 'jira' and account_id = $1",
      [accountId],
    );
    return rows[0];
  }

  test("a stored credential reads back only for its own account", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(1) }));
    await storeCredential(pool, ring, { source: "jira", accountId: id(1) }, { access_token: "a1", refresh_token: "r1" });
    await storeCredential(pool, ring, { source: "jira", accountId: id(1) }, { access_token: "a2", refresh_token: "r2" });
    assert.deepEqual(await loadCredential(pool, ring, { source: "jira", accountId: id(1) }), { access_token: "a2", refresh_token: "r2" });
    assert.equal(await loadCredential(pool, ring, { source: "jira", accountId: id(2) }), null);
    // Copying the row to another account's key doesn't make it that account's credential
    await connectAccount(pool, input({ accountId: id(2), tenant: tenant("globex") }));
    await pool.query(
      `insert into public.source_credentials (source, account_id, kind, key_id, ciphertext)
       select source, $2, kind, key_id, ciphertext from public.source_credentials where source = 'jira' and account_id = $1`,
      [id(1), id(2)],
    );
    await assert.rejects(loadCredential(pool, ring, { source: "jira", accountId: id(2) }), CredentialError);
  });

  test("a credential needs its connection first", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await assert.rejects(storeCredential(pool, ring, { source: "jira", accountId: id(3) }, { access_token: "x" }), /foreign key/);
  });

  test("disconnecting stops the mapping and deletes the credential, and the data stays", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(4) }));
    await storeCredential(pool, ring, { source: "jira", accountId: id(4) }, { access_token: "x" });
    await storeDelivery(id(4), "ingested", tenant("acme"));

    assert.deepEqual(await disconnectAccount(pool, { source: "jira", accountId: id(4), tenant: tenant("acme") }), { ok: true, already: false });
    const row = await connection(id(4));
    assert.equal(row.tenant, tenant("acme"));
    assert.ok(row.disconnected_at instanceof Date);
    assert.equal(await loadCredential(pool, ring, { source: "jira", accountId: id(4) }), null);
    const { rows } = await pool.query("select tenant from public.raw_events where delivery_id = $1", [`ingested-${run}`]);
    assert.equal(rows[0].tenant, tenant("acme"), "what was ingested stays with the tenant");

    // Intake maps only connected accounts (services/intake/intake.py); this is its lookup
    const { rows: mapped } = await pool.query(
      "select tenant from public.installations where source = 'jira' and account_id = $1 and disconnected_at is null",
      [id(4)],
    );
    assert.equal(mapped.length, 0);

    const first = row.disconnected_at;
    assert.deepEqual(await disconnectAccount(pool, { source: "jira", accountId: id(4), tenant: tenant("acme") }), { ok: true, already: true });
    assert.deepEqual((await connection(id(4))).disconnected_at, first, "a second disconnect keeps the first time");
  });

  test("another organization can't disconnect an account, and can't take a disconnected one", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    await connectAccount(pool, input({ accountId: id(5) }));
    assert.deepEqual(await disconnectAccount(pool, { source: "jira", accountId: id(5), tenant: tenant("globex") }), { ok: false, reason: "not-found" });
    assert.equal((await connection(id(5))).disconnected_at, null);

    await disconnectAccount(pool, { source: "jira", accountId: id(5), tenant: tenant("acme") });
    assert.deepEqual(await connectAccount(pool, input({ accountId: id(5), tenant: tenant("globex") })), { ok: false, reason: "other-tenant" });
    assert.deepEqual(await connectAccount(pool, input({ accountId: id(5) })), { ok: true, reconnected: true });
    assert.equal((await connection(id(5))).disconnected_at, null);
  });

  test("Settings lists every source's connections with who connected them", async (t) => {
    if (!ready) return t.skip("ingestion migrations not applied (dbmate up)");
    const own = tenant("listed");
    await connectAccount(pool, input({ accountId: id(6), tenant: own, label: "listed.atlassian.net", userId: "u-6" }));
    await connectAccount(pool, input({ source: "github", accountId: `${run}6`, externalId: `${run}96`, tenant: own, label: "listed-gh" }));
    await pool.query("insert into public.installations (source, account_id, tenant) values ('slack', $1, $2)", [`T${run}`, own]);
    const all = await listConnections(pool, own);
    assert.deepEqual(
      all.map((c) => [c.source, c.label, c.connectedBy]),
      [
        ["github", "listed-gh", "u-1"],
        ["jira", "listed.atlassian.net", "u-6"],
        ["slack", null, null],
      ],
    );
    assert.deepEqual((await listConnections(pool, own, "jira")).map((c) => c.accountId), [id(6)]);
  });
});
