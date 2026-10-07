// Server-side auth (EMBER-51, ADR 0005): email + password today.
// GitHub OAuth (EMBER-12) is added later as a socialProviders entry, and
// organizations (tenants) through Better Auth's organization plugin, without
// changing how sign-in works.

import { betterAuth } from "better-auth";
import { getMigrations } from "better-auth/db/migration";
import { nextCookies } from "better-auth/next-js";
import { createDatabase, DEFAULT_DATABASE_URL } from "./db.ts";
import { sourcesSchema } from "./sources.ts";

export const MIN_PASSWORD_LENGTH = 8;

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
    plugins: [nextCookies()],
  };
}

export function createAuth(settings: AuthSettings = {}) {
  return betterAuth(authOptions(settings));
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
    const db = options.database as { end?: () => Promise<void>; close?: () => void };
    if (typeof db.end === "function") await db.end();
    else db.close?.();
  }
}

const globalAuth = globalThis as unknown as { emberAuth?: ReturnType<typeof createAuth> };

/** The app's single auth instance (one per server process). */
export function getAuth() {
  globalAuth.emberAuth ??= createAuth();
  return globalAuth.emberAuth;
}
