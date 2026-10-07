// Picks the database from DATABASE_URL (EMBER-51, ADR 0005).
//   file:./data/ember.db         -> SQLite (Node's built-in node:sqlite, no native module)
//   postgres://user:pass@host/db -> Postgres (pg Pool)
// Swapping SQLite for Postgres is a change to DATABASE_URL only.

import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import pg from "pg";

export const DEFAULT_DATABASE_URL = "file:./data/ember.db";

export type DatabaseKind = "sqlite" | "postgres";

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

export function createDatabase(url: string): DatabaseSync | pg.Pool {
  if (databaseKind(url) === "sqlite") {
    const path = sqlitePath(url);
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    return new DatabaseSync(path);
  }
  return new pg.Pool({ connectionString: url });
}
