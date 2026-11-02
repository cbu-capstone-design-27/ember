// Server-side auth (EMBER-51, ADR 0006): email + password today.
// GitHub OAuth (EMBER-12) is added later as a socialProviders entry, without
// changing how sign-in works. Organizations (EMBER-124) come from Better Auth's
// organization plugin; each one owns a tenant (lib/organizations.ts).
//
// The CLI signs in through the browser (EMBER-81): `ember auth login` asks for
// a device code, the user approves it on /device, and the CLI receives a
// session token it sends as `Authorization: Bearer <token>`. The flow is in
// the README, under "CLI sign-in".

import { betterAuth, type BetterAuthPlugin } from "better-auth";
import { APIError, createAuthMiddleware } from "better-auth/api";
import { getMigrations } from "better-auth/db/migration";
import { nextCookies } from "better-auth/next-js";
import { bearer, deviceAuthorization, organization } from "better-auth/plugins";
import { closeDatabase, createDatabase, DEFAULT_DATABASE_URL } from "./db.ts";
import { activeMembership, listMemberships, tenantHasData, tenantProblem, type OrgAdapter } from "./organizations.ts";
import { sourcesSchema } from "./sources.ts";

export const MIN_PASSWORD_LENGTH = 8;

/** The only client allowed to ask for a device code: the Ember CLI (EMBER-87). */
export const CLI_CLIENT_ID = "ember-cli";

/**
 * The request half of Better Auth's bearer plugin: `Authorization: Bearer
 * <token>` counts as the session, so the CLI's token works. Its response half
 * is left out on purpose. It copies every new session token, browser sign-ins
 * included, into a `set-auth-token` header that page scripts can read, which
 * the HttpOnly session cookie is there to prevent. The CLI gets its token from
 * the /device/token response body instead.
 */
function bearerRequests(): BetterAuthPlugin {
  const { id, version, hooks } = bearer();
  return { id, version, hooks: { before: hooks.before } };
}

/**
 * Columns Ember adds to Better Auth's `user` table. `sources` is the list of
 * tools the user's team works in (lib/sources.ts), picked during onboarding.
 * SQLite stores it as JSON text, Postgres as jsonb.
 */
export const userFields = {
  sources: {
    type: "string[]",
    required: false,
    defaultValue: [] as string[],
    input: true,
    validator: { input: sourcesSchema },
  },
} as const;

/**
 * Columns Ember adds to the plugin's `organization` table. `tenant` starts as
 * the slug and changes only when an Ember admin links the organization to a
 * tenant that already holds data (linkTenant). Unique: one organization per tenant.
 */
export const organizationFields = {
  tenant: { type: "string", required: true, unique: true, input: false },
} as const;

/** Invite links are sent by hand for now, so they last a week (the plugin's default is 48 hours). */
export const INVITATION_EXPIRES_IN = 60 * 60 * 24 * 7;

export interface AuthSettings {
  databaseUrl?: string;
  secret?: string;
  baseURL?: string;
}

export function authOptions(settings: AuthSettings = {}) {
  const database = createDatabase(settings.databaseUrl ?? process.env.DATABASE_URL ?? DEFAULT_DATABASE_URL);
  return {
    appName: "Ember",
    database,
    secret: settings.secret ?? process.env.BETTER_AUTH_SECRET,
    baseURL: settings.baseURL ?? process.env.BETTER_AUTH_URL,
    emailAndPassword: {
      enabled: true,
      autoSignIn: true, // signing up also signs in
      minPasswordLength: MIN_PASSWORD_LENGTH,
    },
    user: { additionalFields: userFields },
    // Count requests in the database (table rateLimit), not in this process's
    // memory, so every copy of the app shares one count (EMBER-80). Better
    // Auth limits only in production; sign-in and sign-up allow 3 per 10 s per IP.
    rateLimit: { storage: "database" as const },
    hooks: {
      before: createAuthMiddleware(async (ctx) => {
        // A new organization's slug becomes its tenant, so it has to be a valid
        // tenant id that nobody holds: not another organization (an admin may
        // have linked one to it), and not data ingestion already stored.
        if (ctx.path === "/organization/create") {
          const slug = String(ctx.body?.slug ?? "");
          const problem = tenantProblem(slug);
          if (problem) throw APIError.fromStatus("BAD_REQUEST", { message: problem });
          const adapter = ctx.context.adapter as unknown as OrgAdapter;
          const holder = await adapter.findOne({ model: "organization", where: [{ field: "tenant", value: slug }] });
          if (holder || (await tenantHasData(database, slug))) {
            throw APIError.fromStatus("BAD_REQUEST", { message: "That slug is taken. Pick another." });
          }
        }
        if (ctx.path === "/organization/update" && ctx.body?.data?.slug !== undefined) {
          throw APIError.fromStatus("BAD_REQUEST", { message: "The slug can't change: it names the organization's tenant." });
        }
      }),
    },
    plugins: [
      organization({
        invitationExpiresIn: INVITATION_EXPIRES_IN,
        cancelPendingInvitationsOnReInvite: true,
        schema: { organization: { additionalFields: organizationFields } },
        organizationHooks: {
          beforeCreateOrganization: async ({ organization }) => ({ data: { ...organization, tenant: organization.slug } }),
        },
      }),
      // Codes last 30 minutes and the CLI polls every 5 s (the plugin's defaults).
      deviceAuthorization({
        verificationUri: "/device",
        validateClient: (clientId) => clientId === CLI_CLIENT_ID,
      }),
      bearerRequests(),
      nextCookies(), // last, so it sees the cookies the other plugins set
    ],
  };
}

export function createAuth(settings: AuthSettings = {}) {
  return betterAuth(authOptions(settings));
}

export type Auth = ReturnType<typeof createAuth>;

/** The organization a session last switched to (a column the organization plugin adds to `session`). */
export function activeOrganizationId(session: object): string | null {
  return (session as { activeOrganizationId?: string | null }).activeOrganizationId ?? null;
}

export interface CliOrganization {
  name: string;
  slug: string;
  tenant: string;
  role: string;
}

export interface CliUser {
  name: string;
  email: string;
  /** Every organization the user belongs to. */
  organizations: CliOrganization[];
  /** The slug of the one this session works in, or null when the user has none. */
  active: string | null;
}

/**
 * Who a CLI token belongs to (GET /api/whoami), or null when the token is
 * missing, wrong or expired. A browser session cookie works too.
 */
export async function whoami(auth: Auth, headers: Headers): Promise<CliUser | null> {
  const session = await auth.api.getSession({ headers });
  if (!session) return null;
  const { adapter } = await auth.$context;
  const memberships = await listMemberships(adapter as unknown as OrgAdapter, session.user.id);
  const active = activeMembership(memberships, activeOrganizationId(session.session));
  return {
    name: session.user.name,
    email: session.user.email,
    organizations: memberships.map(({ name, slug, tenant, role }) => ({ name, slug, tenant, role })),
    active: active?.slug ?? null,
  };
}

// Better Auth 1.7 creates string[] columns as TEXT on SQLite, then warns on the
// next start that TEXT isn't a JSON type. The column is what it created; only
// that one warning is dropped.
const ARRAY_COLUMN_ON_SQLITE = /^Field \w+ in table \w+ has a different type in the database\. Expected (string|number)\[\] but got TEXT\.$/;

/** Create or update Better Auth's tables. Safe to run on every start. */
export async function migrate(options: ReturnType<typeof authOptions>) {
  const { runMigrations } = await getMigrations({
    ...options,
    logger: {
      log(level, message, ...args) {
        if (ARRAY_COLUMN_ON_SQLITE.test(message)) return;
        const write = level === "error" ? console.error : level === "warn" ? console.warn : console.log;
        write(`[Better Auth] ${message}`, ...args);
      },
    },
  });
  await runMigrations();
}

/**
 * Migrate on a connection of its own, then close it. Run this before the
 * first getAuth(), so the auth instance never sees a database without its
 * tables.
 */
export async function migrateDatabase(settings: AuthSettings = {}) {
  const options = authOptions(settings);
  try {
    await migrate(options);
  } finally {
    await closeDatabase(options.database);
  }
}

const globalAuth = globalThis as unknown as { emberAuth?: Auth };

/** The app's single auth instance (one per server process). */
export function getAuth() {
  globalAuth.emberAuth ??= createAuth();
  return globalAuth.emberAuth;
}
