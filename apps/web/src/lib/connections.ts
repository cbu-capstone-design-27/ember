// Connections (ADR 0010): which source accounts feed a tenant. A connection is
// a row in ingestion's `installations` table (docs/ingestion/architecture.md,
// Storage), the same row intake reads to file a delivery under its tenant.
// The web app writes it once the source has proven the account belongs to the
// person connecting it (lib/github-connect.ts for GitHub).
//
// Postgres only: SQLite has no ingestion tables.

import type pg from "pg";
import { storeCredential, type KeyRing } from "./source-credentials.ts";

/** Seconds between the two hand-offs. Intake gives up on a commit after 8 s. */
export const SECOND_HAND_OFF_DELAY_MS = 10_000;

export interface Connection {
  source: string;
  accountId: string;
  label: string | null;
  addedAt: Date;
  disconnectedAt: Date | null;
  /** The Better Auth user who connected it; null for rows `make map-tenant` wrote. */
  connectedBy: string | null;
}

export interface ConnectInput {
  source: string;
  /** What intake derives from a delivery: for GitHub the installation id. */
  accountId: string;
  tenant: string;
  label: string;
  /** The account behind it, where accountId is not that (GitHub: the user or organization id). */
  externalId: string | null;
  /** The Better Auth user who connected it. */
  userId: string;
}

export type ConnectResult =
  /** `reconnected`: the row existed for this tenant (it may have been disconnected). */
  | { ok: true; reconnected: boolean }
  /** The account, or a reinstall of it, belongs to another tenant. Moving it would split its records. */
  | { ok: false; reason: "other-tenant" };

/**
 * Record the connection, refusing a move to another tenant, then hand the
 * deliveries stored while it was unmapped to enrich. Run `handOff` once more
 * SECOND_HAND_OFF_DELAY_MS later: a delivery intake was storing while this
 * committed can still land without a tenant.
 *
 * With `credential`, the account's credential of the ring's kind is sealed
 * and stored in the same transaction, so enrich never claims a handed-off
 * delivery before the token to fetch it with exists (Slack, EMBER-148).
 */
export async function connectAccount(
  pool: pg.Pool,
  input: ConnectInput,
  credential?: { ring: KeyRing; secret: Record<string, unknown> },
): Promise<ConnectResult> {
  const client = await pool.connect();
  let result: ConnectResult;
  try {
    await client.query("begin");
    // One connect per account at a time, so two tenants can't both pass the check below
    await client.query("select pg_advisory_xact_lock(hashtext('installations:' || $1 || ':' || $2))", [
      input.source,
      input.externalId ?? input.accountId,
    ]);
    const { rows: held } = await client.query<{ tenant: string }>(
      `select tenant from public.installations
       where source = $1 and (account_id = $2 or ($3::text is not null and external_id = $3)) and tenant <> $4
       limit 1`,
      [input.source, input.accountId, input.externalId, input.tenant],
    );
    if (held.length > 0) {
      await client.query("rollback");
      return { ok: false, reason: "other-tenant" };
    }
    const { rows } = await client.query<{ inserted: boolean }>(
      `insert into public.installations (source, account_id, tenant, label, external_id, connected_by)
       values ($1, $2, $3, $4, $5, $6)
       on conflict (source, account_id) do update
         set label = excluded.label, external_id = excluded.external_id,
             connected_by = excluded.connected_by, disconnected_at = null
         where installations.tenant = excluded.tenant
       returning (xmax = 0) as inserted`,
      [input.source, input.accountId, input.tenant, input.label, input.externalId, input.userId],
    );
    // The check above ran under the lock, so the conflict, if any, is this tenant's own row
    result = { ok: true, reconnected: !rows[0]?.inserted };
    if (credential) await storeCredential(client, credential.ring, { source: input.source, accountId: input.accountId }, credential.secret);
    await client.query("commit");
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
  await handOff(pool, input);
  return result;
}

/**
 * Give the account's unmapped deliveries this tenant and wake the enrich
 * workers, as `map-tenant.sql` does. The insert trigger doesn't fire on an
 * update, hence the notify. Returns how many deliveries were handed over.
 */
export async function handOff(pool: pg.Pool, { source, accountId, tenant }: Pick<ConnectInput, "source" | "accountId" | "tenant">): Promise<number> {
  const client = await pool.connect();
  try {
    await client.query("begin");
    const { rowCount } = await client.query(
      `update public.raw_events set tenant = $3
       where source = $1 and account_id = $2 and tenant is null
         and exists (select 1 from public.installations
                     where source = $1 and account_id = $2 and tenant = $3 and disconnected_at is null)`,
      [source, accountId, tenant],
    );
    await client.query("select pg_notify('raw_events', $1)", [source]);
    await client.query("commit");
    return rowCount ?? 0;
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}

/** The second hand-off, for `after()`: waits, then runs, and logs instead of throwing. */
export async function handOffAgainLater(pool: pg.Pool, input: Pick<ConnectInput, "source" | "accountId" | "tenant">, delayMs = SECOND_HAND_OFF_DELAY_MS) {
  await new Promise((resolve) => setTimeout(resolve, delayMs));
  try {
    const count = await handOff(pool, input);
    if (count > 0) console.log(`[connect] ${input.source} ${input.accountId}: ${count} more stored deliveries handed to enrich`);
  } catch (error) {
    console.warn(`[connect] second hand-off for ${input.source} ${input.accountId} failed: ${(error as Error).message}`);
  }
}

/** A tenant's connections, of one source or of all, by source then oldest first. */
export async function listConnections(pool: pg.Pool, tenant: string, source?: string): Promise<Connection[]> {
  const { rows } = await pool.query<{
    source: string;
    account_id: string;
    label: string | null;
    added_at: Date;
    disconnected_at: Date | null;
    connected_by: string | null;
  }>(
    `select source, account_id, label, added_at, disconnected_at, connected_by from public.installations
     where tenant = $1 and ($2::text is null or source = $2) order by source, added_at, account_id`,
    [tenant, source ?? null],
  );
  return rows.map((r) => ({
    source: r.source,
    accountId: r.account_id,
    label: r.label,
    addedAt: r.added_at,
    disconnectedAt: r.disconnected_at,
    connectedBy: r.connected_by,
  }));
}

export type DisconnectResult =
  /** `already`: it was disconnected before, by an owner or by the source's own uninstall event. */
  | { ok: true; already: boolean }
  /** No such connection in this tenant. */
  | { ok: false; reason: "not-found" };

/**
 * Disconnect (ADR 0010): stop ingesting the account without deleting what was
 * stored (EMBER-53). From the next delivery on, intake stores it with no
 * tenant, as it does an unmapped account's. Its credentials are deleted in the
 * same transaction, so enrich can't call the source with them again. Removing
 * the hook on the source's side is the caller's job, per source.
 *
 * Reconnecting the same account to the same tenant (`connectAccount`) clears
 * `disconnected_at`; connecting it to another tenant stays refused.
 */
export async function disconnectAccount(
  pool: pg.Pool,
  { source, accountId, tenant }: Pick<ConnectInput, "source" | "accountId" | "tenant">,
): Promise<DisconnectResult> {
  const client = await pool.connect();
  try {
    await client.query("begin");
    const { rows } = await client.query<{ already: boolean }>(
      `with target as (
         select source, account_id, disconnected_at is not null as already from public.installations
         where source = $1 and account_id = $2 and tenant = $3 for update
       )
       update public.installations i set disconnected_at = coalesce(i.disconnected_at, now())
       from target where i.source = target.source and i.account_id = target.account_id
       returning target.already`,
      [source, accountId, tenant],
    );
    if (rows.length === 0) {
      await client.query("rollback");
      return { ok: false, reason: "not-found" };
    }
    await client.query("delete from public.source_credentials where source = $1 and account_id = $2", [source, accountId]);
    await client.query("commit");
    return { ok: true, already: rows[0].already };
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}

/**
 * Which of these accounts some tenant already holds, connected or not, by
 * account id or by the account behind it. The picker uses it to tell "already
 * yours" from "someone else's" without naming the other tenant.
 */
export async function heldAccounts(
  pool: pg.Pool,
  source: string,
  accounts: { accountId: string; externalId: string | null }[],
): Promise<Map<string, { tenant: string; connected: boolean }>> {
  const held = new Map<string, { tenant: string; connected: boolean }>();
  if (accounts.length === 0) return held;
  const { rows } = await pool.query<{ account_id: string; external_id: string | null; tenant: string; connected: boolean }>(
    `select account_id, external_id, tenant, disconnected_at is null as connected from public.installations
     where source = $1 and (account_id = any($2::text[]) or external_id = any($3::text[]))`,
    [source, accounts.map((a) => a.accountId), accounts.flatMap((a) => (a.externalId ? [a.externalId] : []))],
  );
  for (const account of accounts) {
    // The installation's own row first, then any other installation of the same account
    const own = rows.find((r) => r.account_id === account.accountId);
    const sibling = rows.find((r) => account.externalId !== null && r.external_id === account.externalId);
    const row = own ?? sibling;
    if (row) held.set(account.accountId, { tenant: row.tenant, connected: own ? own.connected : false });
  }
  return held;
}
