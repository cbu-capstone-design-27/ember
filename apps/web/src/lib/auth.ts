// Server-side auth (EMBER-51, ADR 0006): email + password today.
// GitHub OAuth (EMBER-12) is added later as a socialProviders entry, and
// organizations (tenants) through Better Auth's organization plugin, without
// changing how sign-in works.
//
// The CLI signs in through the browser (EMBER-81): `ember auth login` asks for
// a device code, the user approves it on /device, and the CLI receives a
// session token it sends as `Authorization: Bearer <token>`. The flow is in
// the README, under "CLI sign-in".

import { betterAuth, type BetterAuthPlugin } from "better-auth";
import { getMigrations } from "better-auth/db/migration";
import { nextCookies } from "better-auth/next-js";
import { bearer, deviceAuthorization } from "better-auth/plugins";
import { closeDatabase, createDatabase, DEFAULT_DATABASE_URL } from "./db.ts";
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

export interface AuthSettings {
  databaseUrl?: string;
  secret?: string;
  baseURL?: string;
}

export function authOptions(settings: AuthSettings = {}) {
  return {
    appName: "Ember",
    database: createDatabase(settings.databaseUrl ?? process.env.DATABASE_URL ?? DEFAULT_DATABASE_URL),
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
    plugins: [
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

export interface CliUser {
  name: string;
  email: string;
}

/**
 * Who a CLI token belongs to (GET /api/whoami), or null when the token is
 * missing, wrong or expired. A browser session cookie works too.
 */
export async function whoami(auth: Auth, headers: Headers): Promise<CliUser | null> {
  const session = await auth.api.getSession({ headers });
  if (!session) return null;
  return { name: session.user.name, email: session.user.email };
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
