// EMBER-51 acceptance: sign up, log in and log out work, on SQLite and (when
// TEST_POSTGRES_URL is set) on Postgres with no code change. EMBER-81: the CLI
// signs in through the browser with a device code, on both databases too.
// Run: npm test            (SQLite only)
//      TEST_POSTGRES_URL=postgres://... npm test   (both)

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, test } from "node:test";
import { betterAuth } from "better-auth";
import { authOptions, CLI_CLIENT_ID, createAuth, migrate, whoami, type Auth } from "../src/lib/auth.ts";
import {
  closeDatabase,
  createDatabase,
  databaseKind,
  describeDatabase,
  isPostgresDatabase,
  POSTGRES_SCHEMA,
} from "../src/lib/db.ts";
import { formatUserCode, openDeviceRequest } from "../src/lib/device.ts";

const BASE = "http://localhost:3000";
const SECRET = "test-secret-at-least-32-characters-long!!";
const DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code";

async function call(auth: Auth, path: string, init: { body?: unknown; cookie?: string } = {}) {
  const headers = new Headers({ origin: BASE });
  if (init.body !== undefined) headers.set("content-type", "application/json");
  if (init.cookie) headers.set("cookie", init.cookie);
  const response = await auth.handler(
    new Request(`${BASE}/api/auth${path}`, {
      method: init.body === undefined ? "GET" : "POST",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    }),
  );
  const text = await response.text();
  const json = text ? JSON.parse(text) : null;
  const cookie = response.headers
    .getSetCookie()
    .map((c) => c.split(";")[0])
    .join("; ");
  return { status: response.status, json, cookie };
}

/** A POST the way the CLI sends it: JSON, with no Origin and no cookie. */
async function cli(auth: Auth, path: string, body: unknown, headers: Record<string, string> = {}) {
  const response = await auth.handler(
    new Request(`${BASE}/api/auth${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify(body),
    }),
  );
  const text = await response.text();
  return { status: response.status, json: text ? JSON.parse(text) : null };
}

function bearer(token: string) {
  return new Headers({ authorization: `Bearer ${token}` });
}

function suite(label: string, databaseUrl: () => string, cleanup: () => Promise<void> | void) {
  describe(`auth on ${label}`, () => {
    let auth: Auth;
    let migrationOptions: ReturnType<typeof authOptions> | undefined;
    const email = `ada-${Date.now()}@example.com`;
    const password = "correct horse battery";

    after(async () => {
      await closeDatabase(migrationOptions?.database);
      await closeDatabase((auth?.options as { database?: unknown })?.database);
      await cleanup();
    });

    test("tables are created on start", async () => {
      migrationOptions = authOptions({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
      await migrate(migrationOptions);
      await migrate(migrationOptions); // a second start changes nothing
      auth = createAuth({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
    });

    // EMBER-80: on Postgres the web app shares the database with ingestion, whose tables are in public
    test("on Postgres the tables are in their own schema", async (t) => {
      const db = migrationOptions?.database;
      if (!isPostgresDatabase(db)) return t.skip("SQLite has no schemas");
      const { rows } = await db.pool.query<{ table_name: string }>(
        "select table_name from information_schema.tables where table_schema = $1",
        [POSTGRES_SCHEMA],
      );
      assert.deepEqual(rows.map((r) => r.table_name).sort(), ["account", "deviceCode", "rateLimit", "session", "user", "verification"]);
    });

    // Sign-up writes the user, then its password account; a failure between them must leave neither
    test("a failed transaction leaves nothing behind", async () => {
      const { adapter } = await auth.$context;
      const rolledBack = `rolled-back-${Date.now()}@example.com`;
      await assert.rejects(
        adapter.transaction(async (trx) => {
          const now = new Date();
          await trx.create({
            model: "user",
            data: { name: "Rolled back", email: rolledBack, emailVerified: false, createdAt: now, updatedAt: now },
          });
          throw new Error("failed after the user was written");
        }),
        /failed after the user was written/,
      );
      assert.equal(await adapter.findOne({ model: "user", where: [{ field: "email", value: rolledBack }] }), null);
    });

    // Better Auth limits only in production, so this instance turns it on; the count is a database row
    test("sign-in attempts are limited, and counted in the database", async () => {
      const options = authOptions({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
      const limited = betterAuth({ ...options, rateLimit: { ...options.rateLimit, enabled: true } });
      try {
        const attempt = () => call(limited as unknown as Auth, "/sign-in/email", { body: { email, password: "not the password" } });
        const statuses = [];
        for (let i = 0; i < 4; i++) statuses.push((await attempt()).status);
        assert.equal(statuses[3], 429, `3 sign-ins per 10 s, then 429: ${statuses}`);
        const { adapter } = await limited.$context;
        const rows = await adapter.findMany<{ key: string; count: number }>({ model: "rateLimit" });
        assert.ok(rows.some((r) => r.key.endsWith("/sign-in/email") && r.count >= 3), JSON.stringify(rows));
      } finally {
        await closeDatabase(limited.options.database);
      }
    });

    test("sign up creates the account and signs in", async () => {
      const res = await call(auth, "/sign-up/email", { body: { name: "Ada", email, password } });
      assert.equal(res.status, 200, JSON.stringify(res.json));
      assert.equal(res.json.user.email, email);
      assert.ok(res.cookie.includes("better-auth.session_token="), "sign-up sets a session cookie");
      const session = await call(auth, "/get-session", { cookie: res.cookie });
      assert.equal(session.json?.user?.email, email);
    });

    test("signing up twice with the same email is refused", async () => {
      const res = await call(auth, "/sign-up/email", { body: { name: "Ada", email, password } });
      assert.notEqual(res.status, 200);
    });

    test("a short password is refused", async () => {
      const res = await call(auth, "/sign-up/email", {
        body: { name: "Bob", email: `bob-${Date.now()}@example.com`, password: "short" },
      });
      assert.notEqual(res.status, 200);
    });

    test("log in with the right password, refused with the wrong one", async () => {
      const wrong = await call(auth, "/sign-in/email", { body: { email, password: "not the password" } });
      assert.equal(wrong.status, 401);
      const res = await call(auth, "/sign-in/email", { body: { email, password } });
      assert.equal(res.status, 200, JSON.stringify(res.json));
      const session = await call(auth, "/get-session", { cookie: res.cookie });
      assert.equal(session.json?.user?.email, email);
    });

    test("log out ends the session", async () => {
      const login = await call(auth, "/sign-in/email", { body: { email, password } });
      const out = await call(auth, "/sign-out", { body: {}, cookie: login.cookie });
      assert.equal(out.status, 200, JSON.stringify(out.json));
      const session = await call(auth, "/get-session", { cookie: login.cookie });
      assert.equal(session.json, null, "the old cookie no longer has a session");
    });

    test("a new account has no sources until onboarding picks them", async () => {
      const login = await call(auth, "/sign-in/email", { body: { email, password } });
      const session = await call(auth, "/get-session", { cookie: login.cookie });
      assert.deepEqual(session.json.user.sources, []);
    });

    test("sources are saved deduplicated and in catalog order", async () => {
      const login = await call(auth, "/sign-in/email", { body: { email, password } });
      const res = await call(auth, "/update-user", {
        body: { sources: ["slack", "github", "slack"] },
        cookie: login.cookie,
      });
      assert.equal(res.status, 200, JSON.stringify(res.json));
      const session = await call(auth, "/get-session", { cookie: login.cookie });
      assert.deepEqual(session.json.user.sources, ["github", "slack"]);
    });

    test("unknown or empty source lists are refused", async () => {
      const login = await call(auth, "/sign-in/email", { body: { email, password } });
      for (const sources of [["github", "myspace"], [], "github"]) {
        const res = await call(auth, "/update-user", { body: { sources }, cookie: login.cookie });
        assert.equal(res.status, 400, `${JSON.stringify(sources)} should be refused`);
      }
      const session = await call(auth, "/get-session", { cookie: login.cookie });
      assert.deepEqual(session.json.user.sources, ["github", "slack"], "a refused update changes nothing");
    });

    test("sources can be set at sign-up", async () => {
      const res = await call(auth, "/sign-up/email", {
        body: { name: "Grace", email: `grace-${Date.now()}@example.com`, password, sources: ["jira"] },
      });
      assert.equal(res.status, 200, JSON.stringify(res.json));
      const session = await call(auth, "/get-session", { cookie: res.cookie });
      assert.deepEqual(session.json.user.sources, ["jira"]);
    });

    test("passwords are not stored in plain text", async () => {
      const db = auth.options.database as unknown;
      let stored: string;
      if (isPostgresDatabase(db)) {
        const r = await db.pool.query(
          `select password from ${POSTGRES_SCHEMA}.account where "providerId" = 'credential' limit 1`,
        );
        stored = r.rows[0].password;
      } else {
        const row = (db as { prepare: (s: string) => { get: () => { password: string } } })
          .prepare("select password from account where providerId = 'credential' limit 1")
          .get();
        stored = row.password;
      }
      assert.ok(stored && stored !== password && !stored.includes(password));
    });

    describe("CLI sign-in through the browser", () => {
      const requestCode = (clientId = CLI_CLIENT_ID) => cli(auth, "/device/code", { client_id: clientId });
      const poll = (deviceCode: string) =>
        cli(auth, "/device/token", { grant_type: DEVICE_GRANT, device_code: deviceCode, client_id: CLI_CLIENT_ID });

      /** Stand in for time passing: an older last poll, or a code past its expiry. */
      async function age(userCode: string, update: { lastPolledAt?: Date; expiresAt?: Date }) {
        const { adapter } = await auth.$context;
        await adapter.update({ model: "deviceCode", where: [{ field: "userCode", value: userCode }], update });
      }

      /** What /device does: the signed-in user opens the link, then approves or denies. */
      async function review(userCode: string, action: "approve" | "deny") {
        const { cookie } = await call(auth, "/sign-in/email", { body: { email, password } });
        const opened = await call(auth, `/device?user_code=${userCode}`, { cookie });
        assert.equal(opened.status, 200, JSON.stringify(opened.json));
        return call(auth, `/device/${action}`, { body: { userCode }, cookie });
      }

      test("the CLI gets a token once the user approves, and whoami names the user", async () => {
        const code = await requestCode();
        assert.equal(code.status, 200, JSON.stringify(code.json));
        const { device_code, user_code } = code.json;
        assert.equal(code.json.verification_uri, `${BASE}/device`);
        assert.equal(code.json.verification_uri_complete, `${BASE}/device?user_code=${user_code}`);
        assert.equal(code.json.interval, 5);

        const pending = await poll(device_code);
        assert.equal(pending.status, 400);
        assert.equal(pending.json.error, "authorization_pending");

        const approved = await review(user_code, "approve");
        assert.equal(approved.status, 200, JSON.stringify(approved.json));

        await age(user_code, { lastPolledAt: new Date(Date.now() - 60_000) });
        const token = await poll(device_code);
        assert.equal(token.status, 200, JSON.stringify(token.json));
        assert.equal(token.json.token_type, "Bearer");
        assert.ok(token.json.expires_in > 0);
        assert.deepEqual(await whoami(auth, bearer(token.json.access_token)), { name: "Ada", email });
      });

      test("a code gives out one token: using it again is refused", async () => {
        const code = await requestCode();
        await review(code.json.user_code, "approve");
        const first = await poll(code.json.device_code);
        assert.equal(first.status, 200, JSON.stringify(first.json));
        const again = await poll(code.json.device_code);
        assert.equal(again.status, 400);
        assert.equal(again.json.error, "invalid_grant");
      });

      test("a denied request tells the CLI access_denied", async () => {
        const code = await requestCode();
        const denied = await review(code.json.user_code, "deny");
        assert.equal(denied.status, 200, JSON.stringify(denied.json));
        const result = await poll(code.json.device_code);
        assert.equal(result.status, 400);
        assert.equal(result.json.error, "access_denied");
      });

      test("an expired code is refused in the browser and in the CLI", async () => {
        const code = await requestCode();
        await age(code.json.user_code, { expiresAt: new Date(Date.now() - 1000) });
        const { cookie } = await call(auth, "/sign-in/email", { body: { email, password } });
        const opened = await call(auth, `/device?user_code=${code.json.user_code}`, { cookie });
        assert.equal(opened.status, 400);
        assert.equal(opened.json.error, "expired_token");
        assert.deepEqual(await openDeviceRequest(auth, code.json.user_code, new Headers({ cookie })), { state: "expired" });
        const result = await poll(code.json.device_code);
        assert.equal(result.status, 400);
        assert.equal(result.json.error, "expired_token");
      });

      test("the /device page shows a request only to the user who opened it first", async () => {
        const { user_code } = (await requestCode()).json;
        const ada = new Headers({ cookie: (await call(auth, "/sign-in/email", { body: { email, password } })).cookie });
        assert.deepEqual(await openDeviceRequest(auth, user_code, ada), { state: "pending", userCode: user_code });

        const eve = await call(auth, "/sign-up/email", { body: { name: "Eve", email: `eve-${Date.now()}@example.com`, password } });
        const asEve = await openDeviceRequest(auth, user_code, new Headers({ cookie: eve.cookie }));
        assert.deepEqual(asEve, { state: "invalid" }, "someone else sees it as an unknown code");

        await call(auth, "/device/approve", { body: { userCode: user_code }, cookie: ada.get("cookie")! });
        assert.deepEqual(await openDeviceRequest(auth, user_code, ada), { state: "approved" });
        assert.deepEqual(await openDeviceRequest(auth, "NOTACODE", ada), { state: "invalid" });
      });

      test("polling faster than the interval is told to slow down", async () => {
        const code = await requestCode();
        await poll(code.json.device_code);
        const tooSoon = await poll(code.json.device_code);
        assert.equal(tooSoon.status, 400);
        assert.equal(tooSoon.json.error, "slow_down");
      });

      test("only the Ember CLI can ask for a code", async () => {
        const res = await requestCode("someone-else");
        assert.equal(res.status, 400);
        assert.equal(res.json.error, "invalid_client");
      });

      test("signing out with the token revokes it (ember auth logout)", async () => {
        const code = await requestCode();
        await review(code.json.user_code, "approve");
        const token = await poll(code.json.device_code);
        const out = await cli(auth, "/sign-out", {}, { authorization: `Bearer ${token.json.access_token}` });
        assert.equal(out.status, 200, JSON.stringify(out.json));
        assert.equal(await whoami(auth, bearer(token.json.access_token)), null);
      });

      test("whoami refuses a missing or made-up token", async () => {
        assert.equal(await whoami(auth, new Headers()), null);
        assert.equal(await whoami(auth, bearer("not-a-real-token")), null);
      });
    });
  });
}

const dir = mkdtempSync(join(tmpdir(), "ember-web-"));
suite("SQLite", () => `file:${join(dir, "test.db")}`, () => rmSync(dir, { recursive: true, force: true }));

const postgresUrl = process.env.TEST_POSTGRES_URL;
if (postgresUrl) {
  assert.equal(databaseKind(postgresUrl), "postgres");
  suite("Postgres", () => postgresUrl, () => {});
} else {
  test("Postgres suite skipped (set TEST_POSTGRES_URL to run it)", { skip: true }, () => {});
}

test("/device shows the code in two groups of four, as the CLI prints it", () => {
  assert.equal(formatUserCode("abcdefgh"), "ABCD-EFGH");
  assert.equal(formatUserCode("ABCD-EFGH"), "ABCD-EFGH");
});

test("a Postgres connection that drops while idle doesn't crash the app", async () => {
  const db = createDatabase("postgres://u:p@127.0.0.1:1/db"); // the pool connects lazily, so nothing is reached
  assert.ok(isPostgresDatabase(db));
  assert.equal(db.transaction, true);
  assert.doesNotThrow(() => db.pool.emit("error", new Error("terminating connection due to administrator command")));
  await closeDatabase(db);
});

test("the startup log names the database, never its password", () => {
  const postgres = describeDatabase("postgres://ember:s3cret-pw@hv-worker-1.tail470a31.ts.net:5432/ember");
  assert.equal(postgres, "Postgres, schema web, on hv-worker-1.tail470a31.ts.net:5432/ember");
  assert.ok(!postgres.includes("s3cret-pw"));
  assert.match(describeDatabase(undefined), /^SQLite file .*data\/ember\.db \(DATABASE_URL not set\)$/);
  assert.match(describeDatabase("file:/data/ember.db"), /^SQLite file \/data\/ember\.db$/);
});

test("DATABASE_URL picks the database", () => {
  assert.equal(databaseKind("file:./data/ember.db"), "sqlite");
  assert.equal(databaseKind("postgres://u:p@h/db"), "postgres");
  assert.equal(databaseKind("postgresql://u:p@h/db"), "postgres");
  assert.throws(() => databaseKind("mysql://u:p@h/db"), /DATABASE_URL must start with/);
});
