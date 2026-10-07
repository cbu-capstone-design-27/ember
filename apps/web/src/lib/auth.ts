// Server-side auth (EMBER-51, ADR 0005): email + password today.
// GitHub OAuth (EMBER-12) is added later as a socialProviders entry, and
// organizations (tenants) through Better Auth's organization plugin, without
// changing how sign-in works.

import { betterAuth } from "better-auth";
import { getMigrations } from "better-auth/db/migration";
import { nextCookies } from "better-auth/next-js";
import { createDatabase, DEFAULT_DATABASE_URL } from "./db.ts";

export const MIN_PASSWORD_LENGTH = 8;

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
    plugins: [nextCookies()],
  };
}

export function createAuth(settings: AuthSettings = {}) {
  return betterAuth(authOptions(settings));
}

/** Create or update Better Auth's tables. Safe to run on every start. */
export async function migrate(options: ReturnType<typeof authOptions>) {
  const { runMigrations } = await getMigrations(options);
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
