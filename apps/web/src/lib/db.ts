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
  pool: pg.Pool;
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

export function createDatabase(url: string): DatabaseSync | PostgresDatabase {
  if (databaseKind(url) === "sqlite") {
    const path = sqlitePath(url);
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    return new DatabaseSync(path);
  }
  const pool = new pg.Pool({ connectionString: url });
  return { dialect: new PostgresDialect({ pool }), type: "postgres", schemaName: POSTGRES_SCHEMA, pool };
}

/** Close what createDatabase opened. */
export async function closeDatabase(db: unknown) {
  if (db instanceof DatabaseSync) db.close();
  else if (db && typeof db === "object" && "pool" in db && db.pool instanceof pg.Pool) await db.pool.end();
}
