// EMBER-51 acceptance: sign up, log in and log out work, on SQLite and (when
// TEST_POSTGRES_URL is set) on Postgres with no code change.
// Run: npm test            (SQLite only)
//      TEST_POSTGRES_URL=postgres://... npm test   (both)

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, test } from "node:test";
import pg from "pg";
import { authOptions, createAuth, migrate } from "../src/lib/auth.ts";
import { databaseKind } from "../src/lib/db.ts";

const BASE = "http://localhost:3000";
const SECRET = "test-secret-at-least-32-characters-long!!";

type Auth = ReturnType<typeof createAuth>;

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

function suite(label: string, databaseUrl: () => string, cleanup: () => Promise<void> | void) {
  describe(`auth on ${label}`, () => {
    let auth: Auth;
    let migrationOptions: ReturnType<typeof authOptions> | undefined;
    const email = `ada-${Date.now()}@example.com`;
    const password = "correct horse battery";

    async function close(db: unknown) {
      if (db instanceof pg.Pool) await db.end();
      else (db as { close?: () => void } | undefined)?.close?.();
    }

    after(async () => {
      await close(migrationOptions?.database);
      await close((auth?.options as { database?: unknown })?.database);
      await cleanup();
    });

    test("tables are created on start", async () => {
      migrationOptions = authOptions({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
      await migrate(migrationOptions);
      await migrate(migrationOptions); // a second start changes nothing
      auth = createAuth({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
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
      if (db instanceof pg.Pool) {
        const r = await db.query(`select password from account where "providerId" = 'credential' limit 1`);
        stored = r.rows[0].password;
      } else {
        const row = (db as { prepare: (s: string) => { get: () => { password: string } } })
          .prepare("select password from account where providerId = 'credential' limit 1")
          .get();
        stored = row.password;
      }
      assert.ok(stored && stored !== password && !stored.includes(password));
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

test("DATABASE_URL picks the database", () => {
  assert.equal(databaseKind("file:./data/ember.db"), "sqlite");
  assert.equal(databaseKind("postgres://u:p@h/db"), "postgres");
  assert.equal(databaseKind("postgresql://u:p@h/db"), "postgres");
  assert.throws(() => databaseKind("mysql://u:p@h/db"), /DATABASE_URL must start with/);
});
