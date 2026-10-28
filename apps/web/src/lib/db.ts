// Picks the database from DATABASE_URL (EMBER-51, ADR 0006).
//   file:./data/ember.db         -> SQLite (Node's built-in node:sqlite, no native module)
//   postgres://user:pass@host/db -> Postgres (pg Pool), tables in schema `web` (EMBER-80)
// Swapping SQLite for Postgres is a change to DATABASE_URL only.
//
// On Postgres the web app shares the database with ingestion, whose tables
// (raw_events, source_records, ...) are in `public`. Better Auth's tables go in
// their own schema so the two never collide; Better Auth qualifies every
// statement with it and creates it on the first migration.

import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { PostgresDialect } from "kysely";
import pg from "pg";

export const DEFAULT_DATABASE_URL = "file:./data/ember.db";

/** The Postgres schema that holds the web app's tables. */
export const POSTGRES_SCHEMA = "web";

export type DatabaseKind = "sqlite" | "postgres";

/** Better Auth's database option for Postgres, plus the pool so it can be closed. */
export interface PostgresDatabase {
  dialect: PostgresDialect;
  type: "postgres";
  schemaName: string;
  // Better Auth turns transactions on by itself only when handed a bare pool;
  // without this, sign-up could store a user without its password.
  transaction: true;
  pool: pg.Pool;
}

export function isPostgresDatabase(db: unknown): db is PostgresDatabase {
  return !!db && typeof db === "object" && "pool" in db && db.pool instanceof pg.Pool;
}

export function databaseKind(url: string): DatabaseKind {
  if (url.startsWith("file:")) return "sqlite";
  if (url.startsWith("postgres://") || url.startsWith("postgresql://")) return "postgres";
  throw new Error(
    "DATABASE_URL must start with file: (SQLite) or postgres:// (Postgres), " +
      `got ${JSON.stringify(url.split(":")[0] + ":")}`,
  );
}

export function sqlitePath(url: string): string {
  const path = url.slice("file:".length);
  if (!path) throw new Error("DATABASE_URL file: needs a path, e.g. file:./data/ember.db");
  return path === ":memory:" ? path : resolve(path);
}

/**
 * Which database the app uses, for the startup log: never the password, and
 * says so when DATABASE_URL is unset, since then sign-ins land in a local file.
 */
export function describeDatabase(url: string | undefined): string {
  if (!url) return `SQLite file ${sqlitePath(DEFAULT_DATABASE_URL)} (DATABASE_URL not set)`;
  if (databaseKind(url) === "sqlite") return `SQLite file ${sqlitePath(url)}`;
  const { hostname, port, pathname } = new URL(url);
  return `Postgres, schema ${POSTGRES_SCHEMA}, on ${hostname}:${port || "5432"}${pathname}`;
}

export function createDatabase(url: string): DatabaseSync | PostgresDatabase {
  if (databaseKind(url) === "sqlite") {
    const path = sqlitePath(url);
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    return new DatabaseSync(path);
  }
  const pool = new pg.Pool({ connectionString: url });
  // An idle connection that drops (Postgres restarts, the tailnet link blips)
  // is an 'error' event on the pool; with no listener Node would crash. The
  // pool replaces the connection on the next query.
  pool.on("error", (error) => console.warn(`[db] idle Postgres connection closed: ${error.message}`));
  return {
    dialect: new PostgresDialect({ pool }),
    type: "postgres",
    schemaName: POSTGRES_SCHEMA,
    transaction: true,
    pool,
  };
}

/** Close what createDatabase opened. */
export async function closeDatabase(db: unknown) {
  if (db instanceof DatabaseSync) db.close();
  else if (isPostgresDatabase(db)) await db.pool.end();
}
