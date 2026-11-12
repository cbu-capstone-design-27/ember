// EMBER-124 acceptance: organizations are created, joined by invite, switched
// between, and linked to a tenant only by an Ember admin. EMBER-165: every user
// has one personal workspace with its own tenant, which can't invite, be left
// or be linked, and connects sources under its tenant. On SQLite, and on
// Postgres when TEST_POSTGRES_URL is set (with the ingestion migrations
// applied, the tenant-data check runs against them too).
// Run: npm test            (SQLite only)
//      TEST_POSTGRES_URL=postgres://... npm test   (both)

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";
import { authOptions, createAuth, migrate, whoami, type Auth } from "../src/lib/auth.ts";
import { connectAccount } from "../src/lib/connections.ts";
import { closeDatabase, databaseKind, isPostgresDatabase } from "../src/lib/db.ts";
import {
  activeMembership,
  canConnectSources,
  ensurePersonalWorkspace,
  invitationsFor,
  invitationsOf,
  isEmberAdmin,
  linkTenant,
  listMemberships,
  personalTenant,
  slugFromName,
  tenantProblem,
  type OrgAdapter,
} from "../src/lib/organizations.ts";

const BASE = "http://localhost:3000";
const SECRET = "test-secret-at-least-32-characters-long!!";
const PASSWORD = "correct horse battery";

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
  const cookie = response.headers
    .getSetCookie()
    .map((c) => c.split(";")[0])
    .join("; ");
  return { status: response.status, json: text ? JSON.parse(text) : null, cookie };
}

function cookieHeaders(cookie: string) {
  return new Headers({ cookie, origin: BASE });
}

function suite(label: string, databaseUrl: () => string, cleanup: () => Promise<void> | void) {
  describe(`organizations on ${label}`, () => {
    let auth: Auth;
    let adapter: OrgAdapter;
    let migrationOptions: ReturnType<typeof authOptions> | undefined;
    // Postgres keeps rows between runs, so every slug and email is unique to this run
    const run = Date.now().toString(36);
    const users: Record<"owner" | "bob" | "carol" | "admin", { email: string; cookie: string; id: string }> = {
      owner: { email: `owner-${run}@example.com`, cookie: "", id: "" },
      bob: { email: `bob-${run}@example.com`, cookie: "", id: "" },
      carol: { email: `carol-${run}@example.com`, cookie: "", id: "" },
      admin: { email: `admin-${run}@example.com`, cookie: "", id: "" },
    };
    const savedAdmins = process.env.EMBER_ADMIN_EMAILS;
    let orgId = "";

    before(async () => {
      migrationOptions = authOptions({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
      await migrate(migrationOptions);
      auth = createAuth({ databaseUrl: databaseUrl(), secret: SECRET, baseURL: BASE });
      adapter = (await auth.$context).adapter as unknown as OrgAdapter;
      for (const [name, user] of Object.entries(users)) {
        const res = await call(auth, "/sign-up/email", { body: { name, email: user.email, password: PASSWORD } });
        assert.equal(res.status, 200, JSON.stringify(res.json));
        user.cookie = res.cookie;
        user.id = res.json.user.id;
      }
      process.env.EMBER_ADMIN_EMAILS = `someone@example.com, ${users.admin.email.toUpperCase()}`;
    });

    after(async () => {
      process.env.EMBER_ADMIN_EMAILS = savedAdmins;
      await closeDatabase(migrationOptions?.database);
      await closeDatabase((auth?.options as { database?: unknown })?.database);
      await cleanup();
    });

    test("a new user gets a personal workspace at sign-up, and works in it", async () => {
      const tenant = personalTenant(users.owner.id);
      // Read straight from the table: listMemberships would create it if sign-up hadn't
      const org = await adapter.findOne<{ name: string; tenant: string; personal: boolean }>({
        model: "organization",
        where: [{ field: "id", value: tenant }],
      });
      assert.deepEqual({ name: org?.name, tenant: org?.tenant, personal: org?.personal }, { name: "Personal", tenant, personal: true });
      assert.deepEqual(await listMemberships(adapter, users.owner.id), [
        { id: tenant, name: "Personal", slug: tenant, tenant, role: "owner", personal: true },
      ]);
      const who = await whoami(auth, cookieHeaders(users.owner.cookie));
      assert.equal(who?.active, tenant);
    });

    test("a user from before personal workspaces gets exactly one, even from concurrent loads", async () => {
      // Created on the adapter, so no sign-up hook ran
      const user = await adapter.create<{ id: string }>({
        model: "user",
        data: { name: "Old", email: `old-${run}@example.com`, emailVerified: false, createdAt: new Date(), updatedAt: new Date() },
      });
      assert.equal(await adapter.findOne({ model: "member", where: [{ field: "userId", value: user.id }] }), null);
      const loads = await Promise.all([
        ...Array.from({ length: 4 }, () => listMemberships(adapter, user.id)),
        ensurePersonalWorkspace(adapter, user.id).then(() => listMemberships(adapter, user.id)),
      ]);
      for (const memberships of loads) assert.deepEqual(memberships.map((m) => [m.tenant, m.personal]), [[personalTenant(user.id), true]]);
      const members = await adapter.findMany({ model: "member", where: [{ field: "userId", value: user.id }] });
      assert.equal(members.length, 1);
    });

    test("nobody can be invited to, added to, removed from, or leave a personal workspace", async () => {
      const personal = personalTenant(users.carol.id);
      const invite = await call(auth, "/organization/invite-member", {
        body: { email: users.bob.email, role: "member", organizationId: personal },
        cookie: users.carol.cookie,
      });
      assert.equal(invite.status, 403, JSON.stringify(invite.json));
      await assert.rejects(auth.api.addMember({ body: { userId: users.bob.id, role: "member", organizationId: personal } }));
      for (const path of ["/organization/leave", "/organization/delete"]) {
        const res = await call(auth, path, { body: { organizationId: personal }, cookie: users.carol.cookie });
        assert.equal(res.status, 403, `${path}: ${JSON.stringify(res.json)}`);
      }
      const removed = await call(auth, "/organization/remove-member", {
        body: { memberIdOrEmail: users.carol.email, organizationId: personal },
        cookie: users.carol.cookie,
      });
      // The plugin's only-owner check refuses this before Ember's hook does
      assert.equal(removed.status, 400, JSON.stringify(removed.json));
      assert.deepEqual(
        (await listMemberships(adapter, users.carol.id)).map((m) => m.id),
        [personal],
      );
    });

    test("a personal workspace can't be linked, and organizations can't take a personal tenant", async () => {
      const personal = personalTenant(users.carol.id);
      const linked = await linkTenant(adapter, { actorEmail: users.admin.email, organizationId: personal, tenant: `ember-dev-other-${run}` });
      assert.deepEqual(linked, { ok: false, error: "A personal workspace keeps its own tenant." });
      const create = await call(auth, "/organization/create", { body: { name: "Squatter", slug: personalTenant(users.bob.id) }, cookie: users.carol.cookie });
      assert.equal(create.status, 400);
      const reserved = await call(auth, "/organization/create", { body: { name: "Squatter", slug: `u-${run}` }, cookie: users.carol.cookie });
      assert.equal(reserved.status, 400);
    });

    test("connecting a source in the personal workspace files it under the personal tenant", async (t) => {
      const db = migrationOptions?.database;
      if (!isPostgresDatabase(db)) return t.skip("SQLite has no installations table");
      const { rows } = await db.pool.query<{ exists: boolean }>("select to_regclass('public.installations') is not null as exists");
      if (!rows[0]?.exists) return t.skip("ingestion migrations not applied (dbmate up)");
      // What connector() (app/connect/server.ts) does to pick the membership every connect flow files under
      const membership = (await listMemberships(adapter, users.carol.id)).find((m) => m.id === personalTenant(users.carol.id));
      assert.ok(membership && canConnectSources(membership.role), "the owner may connect");
      const accountId = `personal-${run}`;
      try {
        const result = await connectAccount(db.pool, {
          source: "github",
          accountId,
          tenant: membership.tenant,
          label: "carol",
          externalId: null,
          userId: users.carol.id,
        });
        assert.equal(result.ok, true, JSON.stringify(result));
        const { rows: installed } = await db.pool.query<{ tenant: string }>(
          "select tenant from public.installations where source = 'github' and account_id = $1",
          [accountId],
        );
        assert.deepEqual(installed, [{ tenant: personalTenant(users.carol.id) }]);
      } finally {
        await db.pool.query("delete from public.installations where source = 'github' and account_id = $1", [accountId]);
      }
    });

    test("creating an organization makes the creator its owner, with the slug as tenant", async () => {
      const slug = `kestrel-${run}`;
      const res = await call(auth, "/organization/create", { body: { name: "Kestrel", slug }, cookie: users.owner.cookie });
      assert.equal(res.status, 200, JSON.stringify(res.json));
      assert.equal(res.json.tenant, slug);
      orgId = res.json.id;
      const memberships = await listMemberships(adapter, users.owner.id);
      assert.deepEqual(memberships.slice(1), [{ id: orgId, name: "Kestrel", slug, tenant: slug, role: "owner", personal: false }]);
      const who = await whoami(auth, cookieHeaders(users.owner.cookie));
      assert.equal(who?.active, slug);
    });

    test("a slug that isn't a valid tenant id is refused", async () => {
      for (const slug of ["Upper", "-dash-first", "under_score", "", "a".repeat(64)]) {
        const res = await call(auth, "/organization/create", { body: { name: "Bad", slug }, cookie: users.owner.cookie });
        assert.equal(res.status, 400, `slug ${JSON.stringify(slug)}: ${JSON.stringify(res.json)}`);
      }
    });

    test("a slug another organization uses is refused", async () => {
      const res = await call(auth, "/organization/create", {
        body: { name: "Copy", slug: `kestrel-${run}` },
        cookie: users.bob.cookie,
      });
      assert.equal(res.status, 400);
    });

    test("the tenant can't be set when creating", async () => {
      const slug = `sneaky-${run}`;
      const res = await call(auth, "/organization/create", {
        body: { name: "Sneaky", slug, tenant: "ember-development" },
        cookie: users.carol.cookie,
      });
      // The plugin rejects unknown body fields, or ignores them; either way the tenant is the slug
      if (res.status === 200) assert.equal(res.json.tenant, slug);
      else assert.equal(res.status, 400);
    });

    test("the slug can't change, the name can", async () => {
      const slugChange = await call(auth, "/organization/update", {
        body: { organizationId: orgId, data: { slug: `renamed-${run}` } },
        cookie: users.owner.cookie,
      });
      assert.equal(slugChange.status, 400);
      const nameChange = await call(auth, "/organization/update", {
        body: { organizationId: orgId, data: { name: "Kestrel Labs" } },
        cookie: users.owner.cookie,
      });
      assert.equal(nameChange.status, 200, JSON.stringify(nameChange.json));
      assert.equal(nameChange.json.slug, `kestrel-${run}`);
    });

    test("an invited user sees the invite when they log in, and joins by accepting it", async () => {
      const invite = await call(auth, "/organization/invite-member", {
        body: { email: users.bob.email, role: "member", organizationId: orgId },
        cookie: users.owner.cookie,
      });
      assert.equal(invite.status, 200, JSON.stringify(invite.json));

      const waiting = await invitationsFor(auth, users.bob.email);
      assert.deepEqual(
        waiting.map((i) => [i.id, i.organizationName, i.role]),
        [[invite.json.id, "Kestrel Labs", "member"]],
      );
      const listed = await invitationsOf(auth, orgId, cookieHeaders(users.owner.cookie));
      assert.deepEqual(listed.map((i) => i.email), [users.bob.email]);

      const accepted = await call(auth, "/organization/accept-invitation", {
        body: { invitationId: invite.json.id },
        cookie: users.bob.cookie,
      });
      assert.equal(accepted.status, 200, JSON.stringify(accepted.json));
      assert.deepEqual(
        (await listMemberships(adapter, users.bob.id)).map((m) => [m.id, m.role]),
        [
          [personalTenant(users.bob.id), "owner"],
          [orgId, "member"],
        ],
      );
      assert.deepEqual(await invitationsFor(auth, users.bob.email), []);
    });

    test("only the invited email can accept an invite", async () => {
      const invite = await call(auth, "/organization/invite-member", {
        body: { email: `nobody-${run}@example.com`, role: "member", organizationId: orgId },
        cookie: users.owner.cookie,
      });
      assert.equal(invite.status, 200, JSON.stringify(invite.json));
      const stolen = await call(auth, "/organization/accept-invitation", {
        body: { invitationId: invite.json.id },
        cookie: users.carol.cookie,
      });
      assert.equal(stolen.status, 403);
    });

    test("members can't invite; owners and admins can", async () => {
      const res = await call(auth, "/organization/invite-member", {
        body: { email: users.carol.email, role: "member", organizationId: orgId },
        cookie: users.bob.cookie,
      });
      assert.equal(res.status, 403);
    });

    test("a revoked invite can't be accepted", async () => {
      const invite = await call(auth, "/organization/invite-member", {
        body: { email: users.carol.email, role: "admin", organizationId: orgId },
        cookie: users.owner.cookie,
      });
      assert.equal(invite.status, 200, JSON.stringify(invite.json));
      const revoked = await call(auth, "/organization/cancel-invitation", {
        body: { invitationId: invite.json.id },
        cookie: users.owner.cookie,
      });
      assert.equal(revoked.status, 200, JSON.stringify(revoked.json));
      assert.deepEqual(await invitationsFor(auth, users.carol.email), []);
      const accepted = await call(auth, "/organization/accept-invitation", {
        body: { invitationId: invite.json.id },
        cookie: users.carol.cookie,
      });
      assert.equal(accepted.status, 400);
    });

    test("an expired invite is hidden and can't be accepted", async () => {
      const invite = await call(auth, "/organization/invite-member", {
        body: { email: users.carol.email, role: "member", organizationId: orgId },
        cookie: users.owner.cookie,
      });
      assert.equal(invite.status, 200, JSON.stringify(invite.json));
      const expires = new Date(invite.json.expiresAt).getTime() - Date.now();
      assert.ok(expires > 6 * 24 * 3600_000, "invites last a week");
      await adapter.update({
        model: "invitation",
        where: [{ field: "id", value: invite.json.id }],
        update: { expiresAt: new Date(Date.now() - 1000) },
      });
      assert.deepEqual(await invitationsFor(auth, users.carol.email), []);
      const accepted = await call(auth, "/organization/accept-invitation", {
        body: { invitationId: invite.json.id },
        cookie: users.carol.cookie,
      });
      assert.equal(accepted.status, 400);
    });

    test("a user in two organizations switches between them, and the choice persists", async () => {
      const second = await call(auth, "/organization/create", {
        body: { name: "Photon", slug: `photon-${run}`, keepCurrentActiveOrganization: true },
        cookie: users.bob.cookie,
      });
      assert.equal(second.status, 200, JSON.stringify(second.json));
      const memberships = await listMemberships(adapter, users.bob.id);
      assert.deepEqual(memberships.map((m) => m.slug), [personalTenant(users.bob.id), `kestrel-${run}`, `photon-${run}`]);

      const switched = await call(auth, "/organization/set-active", {
        body: { organizationId: second.json.id },
        cookie: users.bob.cookie,
      });
      assert.equal(switched.status, 200, JSON.stringify(switched.json));
      // A fresh read of the session, as the next page load does
      const session = await call(auth, "/get-session", { cookie: users.bob.cookie });
      assert.equal(session.json.session.activeOrganizationId, second.json.id);
      assert.equal((await whoami(auth, cookieHeaders(users.bob.cookie)))?.active, `photon-${run}`);
    });

    test("an organization the user left is no longer active; their first shared one is, else their personal one", () => {
      const personal = { id: "u-1", name: "Personal", slug: "u-1", tenant: "u-1", role: "owner", personal: true };
      const memberships = [
        personal,
        { id: "a", name: "A", slug: "a", tenant: "a", role: "member", personal: false },
        { id: "b", name: "B", slug: "b", tenant: "b", role: "owner", personal: false },
      ];
      assert.equal(activeMembership(memberships, "b")?.id, "b");
      assert.equal(activeMembership(memberships, "u-1")?.id, "u-1");
      assert.equal(activeMembership(memberships, "gone")?.id, "a");
      assert.equal(activeMembership(memberships, null)?.id, "a");
      assert.equal(activeMembership([personal], null)?.id, "u-1");
      assert.equal(activeMembership([], "a"), null);
    });

    test("you can't switch to an organization you aren't in", async () => {
      const res = await call(auth, "/organization/set-active", { body: { organizationId: orgId }, cookie: users.carol.cookie });
      assert.equal(res.status, 403);
    });

    test("only an Ember admin can link an organization to an existing tenant", async () => {
      const tenant = `ember-dev-${run}`;
      const byOwner = await linkTenant(adapter, { actorEmail: users.owner.email, organizationId: orgId, tenant });
      assert.deepEqual(byOwner, { ok: false, error: "Only Ember admins can link a tenant." });

      const byAdmin = await linkTenant(adapter, { actorEmail: users.admin.email, organizationId: orgId, tenant });
      assert.deepEqual(byAdmin, { ok: true });
      const membership = (await listMemberships(adapter, users.owner.id)).find((m) => m.id === orgId);
      assert.equal(membership?.tenant, tenant);
      assert.equal(membership?.slug, `kestrel-${run}`, "linking keeps the slug");
    });

    test("a tenant links to one organization at most", async () => {
      const photon = (await listMemberships(adapter, users.bob.id)).find((m) => m.slug === `photon-${run}`);
      assert.ok(photon);
      const res = await linkTenant(adapter, { actorEmail: users.admin.email, organizationId: photon.id, tenant: `ember-dev-${run}` });
      assert.deepEqual(res, { ok: false, error: `ember-dev-${run} is already linked to Kestrel Labs.` });
      // Nor can a new organization take a linked tenant as its slug
      const create = await call(auth, "/organization/create", {
        body: { name: "Copycat", slug: `ember-dev-${run}` },
        cookie: users.carol.cookie,
      });
      assert.equal(create.status, 400);
      // The database refuses it too, whatever the code checks
      await assert.rejects(
        adapter.update({ model: "organization", where: [{ field: "id", value: photon.id }], update: { tenant: `ember-dev-${run}` } }),
      );
    });

    test("an invalid tenant id can't be linked", async () => {
      const res = await linkTenant(adapter, { actorEmail: users.admin.email, organizationId: orgId, tenant: "Not Valid" });
      assert.equal(res.ok, false);
    });

    test("a slug naming a tenant ingestion already holds data for is refused", async (t) => {
      const db = migrationOptions?.database;
      if (!isPostgresDatabase(db)) return t.skip("SQLite has no ingestion tables");
      const { rows } = await db.pool.query<{ exists: boolean }>("select to_regclass('public.installations') is not null as exists");
      if (!rows[0]?.exists) return t.skip("ingestion migrations not applied (dbmate up)");
      // One row in each table that holds a tenant, alone: a mapped account, a
      // delivery enrich hasn't reached yet, an enriched record
      const holders = {
        installations: "insert into public.installations (source, account_id, tenant) values ('github', $1, $2)",
        raw_events: `insert into public.raw_events (source, delivery_id, tenant, event_name, origin, envelope)
                     values ('github', $1, $2, 'push', 'webhook', '{}')`,
        source_records: `insert into public.source_records (source, tenant, kind, object_id, version, occurred_at, record)
                         values ('github', $2, 'issue', $1, '1', now(), '{}')`,
      };
      for (const [table, insert] of Object.entries(holders)) {
        const key = `${table}-${run}`;
        const tenant = `ingested-${key}`.replace(/_/g, "-");
        await db.pool.query(insert, [key, tenant]);
        try {
          const res = await call(auth, "/organization/create", { body: { name: "Squatter", slug: tenant }, cookie: users.carol.cookie });
          assert.equal(res.status, 400, `a tenant only in ${table} was taken: ${JSON.stringify(res.json)}`);
        } finally {
          await db.pool.query(`delete from public.${table} where tenant = $1`, [tenant]);
        }
      }
    });
  });
}

describe("organization helpers", () => {
  test("tenant ids follow the ontology's subgraph_id rule", () => {
    assert.equal(tenantProblem("ember-development"), null);
    assert.equal(tenantProblem("0day"), null);
    assert.notEqual(tenantProblem("Ember"), null);
    assert.notEqual(tenantProblem("-ember"), null);
    assert.notEqual(tenantProblem("ember_dev"), null);
    assert.notEqual(tenantProblem(""), null);
    assert.notEqual(tenantProblem("a".repeat(64)), null);
  });

  test("personal tenants are valid, deterministic and distinct", () => {
    const ids = ["AbC123", "abc123", "x", "user-with-a-very-long-id".repeat(10)];
    const tenants = ids.map(personalTenant);
    for (const tenant of tenants) {
      assert.equal(tenantProblem(tenant), null, tenant);
      assert.match(tenant, /^u-[0-9a-f]{32}$/);
    }
    assert.equal(new Set(tenants).size, ids.length, "ids differing only in case get different tenants");
    assert.equal(personalTenant("AbC123"), tenants[0]);
  });

  test("a slug is suggested from the name", () => {
    assert.equal(slugFromName("Kestrel Labs!"), "kestrel-labs");
    assert.equal(slugFromName("  Café  Ünïcode  "), "cafe-unicode");
    assert.equal(slugFromName("---"), "");
    assert.equal(tenantProblem(slugFromName(`${"x".repeat(62)} y`)), null);
  });

  test("Ember admins come from EMBER_ADMIN_EMAILS, case-insensitive", () => {
    assert.equal(isEmberAdmin("ada@example.com", "ada@example.com"), true);
    assert.equal(isEmberAdmin("ADA@example.com", " bob@example.com , ada@example.com "), true);
    assert.equal(isEmberAdmin("eve@example.com", "ada@example.com"), false);
    assert.equal(isEmberAdmin("ada@example.com", undefined), false);
    assert.equal(isEmberAdmin("ada@example.com", ""), false);
  });
});

const dir = mkdtempSync(join(tmpdir(), "ember-web-orgs-"));
suite("SQLite", () => `file:${join(dir, "test.db")}`, () => rmSync(dir, { recursive: true, force: true }));

const postgresUrl = process.env.TEST_POSTGRES_URL;
if (postgresUrl) {
  assert.equal(databaseKind(postgresUrl), "postgres");
  suite("Postgres", () => postgresUrl, () => {});
} else {
  test("Postgres suite skipped (set TEST_POSTGRES_URL to run it)", { skip: true }, () => {});
}
