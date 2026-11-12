// Organizations (EMBER-124). An organization is a project: its members share
// one tenant, the id ingestion files every record under (`raw_events.tenant`,
// `source_records.tenant`, the `{tenant}_{source}` subgraphs). A person can
// belong to several. Better Auth's organization plugin stores them (tables
// `organization`, `member`, `invitation`); this module adds the tenant checks
// (the id rules themselves are in tenant.ts), the active organization, and
// linking an organization to a tenant that already holds data.
//
// Every user also has a personal workspace (EMBER-165): an organization with
// `personal` set, the user as its only member and owner, and a tenant of its
// own. It is created at sign-up, or on the next session load for older users,
// and it can't invite, take members, lose its member, be deleted or be linked.

import { createHash } from "node:crypto";
import type { Auth } from "./auth.ts";
import { isPostgresDatabase } from "./db.ts";
import { tenantProblem } from "./tenant.ts";

export { slugFromName, TENANT_MAX_LENGTH, TENANT_PATTERN, tenantProblem } from "./tenant.ts";

/**
 * Ember admins: the emails in EMBER_ADMIN_EMAILS, comma-separated. Only they
 * can link an organization to an existing tenant, because the link hands over
 * every record stored under it.
 */
export function isEmberAdmin(email: string, setting = process.env.EMBER_ADMIN_EMAILS): boolean {
  if (!setting) return false;
  const wanted = email.trim().toLowerCase();
  return setting.split(",").some((entry) => entry.trim().toLowerCase() === wanted);
}

export function canInvite(role: string): boolean {
  return role.split(",").some((r) => r === "owner" || r === "admin");
}

/** Whether the organization `organizationId` is a personal workspace (false when it doesn't exist). */
export async function isPersonalOrganization(adapter: OrgAdapter, organizationId: string): Promise<boolean> {
  const org = await adapter.findOne<OrganizationRow>({ model: "organization", where: [{ field: "id", value: organizationId }] });
  return !!org?.personal;
}

/** Owners and admins connect and disconnect sources (ADR 0010); members see what is connected. */
export function canConnectSources(role: string): boolean {
  return canInvite(role);
}

/**
 * Whether ingestion already files data under `tenant` (Postgres only; SQLite
 * has no ingestion tables). A new organization can't take such a tenant by
 * choosing its slug: only an Ember admin can link to it. `raw_events` counts
 * too, so deliveries enrich hasn't reached yet already hold their tenant. It
 * has no index on `tenant`, which is fine for a check that runs once per new
 * organization.
 */
export async function tenantHasData(db: unknown, tenant: string): Promise<boolean> {
  if (!isPostgresDatabase(db)) return false;
  const { rows: tables } = await db.pool.query<{ name: string }>(
    "select name from unnest(array['installations', 'raw_events', 'source_records']) as name where to_regclass('public.' || name) is not null",
  );
  for (const { name } of tables) {
    // `name` comes from the fixed list above, never from the request
    const { rowCount } = await db.pool.query(`select 1 from public.${name} where tenant = $1 limit 1`, [tenant]);
    if (rowCount) return true;
  }
  return false;
}

/** Tenant ids starting with this belong to personal workspaces, so organizations can't take them. */
export const PERSONAL_TENANT_PREFIX = "u-";

/** The name a personal workspace is created with and shown under. */
export const PERSONAL_NAME = "Personal";

/**
 * The tenant of `userId`'s personal workspace, also its organization id, slug
 * and owner's member id: `u-` plus the first 32 hex characters (128 bits) of
 * the SHA-256 of the user id. Deterministic, so a second create hits the same
 * primary keys; lowercase and 34 characters, so it passes tenantProblem.
 */
export function personalTenant(userId: string): string {
  return PERSONAL_TENANT_PREFIX + createHash("sha256").update(userId).digest("hex").slice(0, 32);
}

export interface Membership {
  id: string;
  name: string;
  slug: string;
  tenant: string;
  role: string;
  /** The user's personal workspace rather than a shared organization. */
  personal: boolean;
}

interface OrganizationRow {
  id: string;
  name: string;
  slug: string;
  tenant: string;
  personal?: boolean | null;
}

interface MemberRow {
  organizationId: string;
  role: string;
  createdAt: Date;
}

/** The slice of Better Auth's adapter this module uses. */
export interface OrgAdapter {
  findMany<T>(data: {
    model: string;
    where?: { field: string; value: string | string[]; operator?: "in" | "eq" | "ne" }[];
    sortBy?: { field: string; direction: "asc" | "desc" };
  }): Promise<T[]>;
  findOne<T>(data: { model: string; where: { field: string; value: string }[] }): Promise<T | null>;
  update<T>(data: { model: string; where: { field: string; value: string }[]; update: Record<string, unknown> }): Promise<T | null>;
  create<T>(data: { model: string; data: Record<string, unknown>; forceAllowId?: boolean }): Promise<T>;
}

/**
 * Create `userId`'s personal workspace unless it exists. Safe to call
 * concurrently: both rows have deterministic ids (personalTenant), so a
 * racing second insert fails on the primary key and the check below finds the
 * first one's rows.
 */
export async function ensurePersonalWorkspace(adapter: OrgAdapter, userId: string): Promise<void> {
  const id = personalTenant(userId);
  const createdAt = new Date();
  const failures: unknown[] = [];
  await adapter
    .create({
      model: "organization",
      data: { id, name: PERSONAL_NAME, slug: id, tenant: id, personal: true, createdAt },
      forceAllowId: true,
    })
    .catch((error: unknown) => failures.push(error));
  await adapter
    .create({ model: "member", data: { id, organizationId: id, userId, role: "owner", createdAt }, forceAllowId: true })
    .catch((error: unknown) => failures.push(error));
  const [org, member] = await Promise.all([
    adapter.findOne<OrganizationRow>({ model: "organization", where: [{ field: "id", value: id }] }),
    adapter.findOne<MemberRow>({ model: "member", where: [{ field: "id", value: id }] }),
  ]);
  if (!org?.personal || !member) throw failures[0] ?? new Error(`personal workspace ${id} is incomplete`);
}

/**
 * Every organization `userId` belongs to: their personal workspace first,
 * then the others, oldest membership first. Creates the personal workspace
 * when it's missing (users from before EMBER-165).
 */
export async function listMemberships(adapter: OrgAdapter, userId: string): Promise<Membership[]> {
  let memberships = await readMemberships(adapter, userId);
  if (!memberships.some((m) => m.personal)) {
    await ensurePersonalWorkspace(adapter, userId);
    memberships = await readMemberships(adapter, userId);
  }
  return memberships.sort((a, b) => Number(b.personal) - Number(a.personal));
}

async function readMemberships(adapter: OrgAdapter, userId: string): Promise<Membership[]> {
  const members = await adapter.findMany<MemberRow>({
    model: "member",
    where: [{ field: "userId", value: userId }],
    sortBy: { field: "createdAt", direction: "asc" },
  });
  if (members.length === 0) return [];
  const organizations = await adapter.findMany<OrganizationRow>({
    model: "organization",
    where: [{ field: "id", operator: "in", value: members.map((m) => m.organizationId) }],
  });
  const byId = new Map(organizations.map((o) => [o.id, o]));
  return members.flatMap((m) => {
    const org = byId.get(m.organizationId);
    return org ? [{ id: org.id, name: org.name, slug: org.slug, tenant: org.tenant, role: m.role, personal: !!org.personal }] : [];
  });
}

/**
 * The organization a session works in: the one it last switched to, while the
 * user still belongs to it, else their oldest shared organization, else their
 * personal workspace. Null only for a list without one (never from listMemberships).
 */
export function activeMembership(memberships: Membership[], activeOrganizationId: string | null | undefined) {
  return memberships.find((m) => m.id === activeOrganizationId) ?? memberships.find((m) => !m.personal) ?? memberships[0] ?? null;
}

export type LinkResult = { ok: true } | { ok: false; error: string };

/**
 * Point an organization at an existing tenant, such as `ember-development`,
 * so its members see the data stored there. Ember admins only. A tenant
 * belongs to one organization at most (the column is unique too).
 */
export async function linkTenant(
  adapter: OrgAdapter,
  { actorEmail, organizationId, tenant }: { actorEmail: string; organizationId: string; tenant: string },
): Promise<LinkResult> {
  if (!isEmberAdmin(actorEmail)) return { ok: false, error: "Only Ember admins can link a tenant." };
  const problem = tenantProblem(tenant);
  if (problem) return { ok: false, error: problem };
  if (tenant.startsWith(PERSONAL_TENANT_PREFIX)) return { ok: false, error: `Tenants starting with ${PERSONAL_TENANT_PREFIX} belong to personal workspaces.` };
  const org = await adapter.findOne<OrganizationRow>({ model: "organization", where: [{ field: "id", value: organizationId }] });
  if (!org) return { ok: false, error: "That organization no longer exists." };
  if (org.personal) return { ok: false, error: "A personal workspace keeps its own tenant." };
  if (org.tenant === tenant) return { ok: true };
  const holder = await adapter.findOne<OrganizationRow>({ model: "organization", where: [{ field: "tenant", value: tenant }] });
  if (holder) return { ok: false, error: `${tenant} is already linked to ${holder.name}.` };
  await adapter.update({ model: "organization", where: [{ field: "id", value: organizationId }], update: { tenant } });
  return { ok: true };
}

export interface PendingInvitation {
  id: string;
  email: string;
  role: string;
  organizationId: string;
  organizationName?: string;
  expiresAt: Date;
}

function stillOpen<T extends { status: string; expiresAt: Date | string }>(invitations: T[], now = new Date()) {
  return invitations
    .filter((i) => i.status === "pending" && new Date(i.expiresAt) > now)
    .map((i) => ({ ...i, expiresAt: new Date(i.expiresAt) }));
}

/**
 * Open invitations addressed to `email`, shown when that person logs in.
 * Looked up on the server: the plugin's own endpoint refuses accounts whose
 * email isn't verified, and Ember doesn't verify emails yet.
 */
export async function invitationsFor(auth: Auth, email: string): Promise<PendingInvitation[]> {
  const invitations = await auth.api.listUserInvitations({ query: { email } });
  return stillOpen(invitations);
}

/** An organization's open invitations, for its owners and admins (the plugin checks the role). */
export async function invitationsOf(auth: Auth, organizationId: string, headers: Headers): Promise<PendingInvitation[]> {
  const invitations = await auth.api.listInvitations({ query: { organizationId }, headers });
  return stillOpen(invitations);
}
