// Server-side session lookups for pages and layouts. Each request reads the
// session once (React cache), however many components ask for it.

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";
import { activeOrganizationId, getAuth } from "./auth.ts";
import { activeMembership, listMemberships, type Membership, type OrgAdapter } from "./organizations.ts";
import { userSources, type SourceId } from "./sources.ts";

export interface CurrentUser {
  id: string;
  name: string;
  email: string;
  sources: SourceId[];
  createdAt: Date;
}

const getSession = cache(async () => {
  // Read the request first: it marks the page dynamic, so the build never
  // creates the auth instance (which needs the runtime secret) to prerender.
  const requestHeaders = await headers();
  return getAuth().api.getSession({ headers: requestHeaders });
});

export const getCurrentUser = cache(async (): Promise<CurrentUser | null> => {
  const session = await getSession();
  if (!session) return null;
  const { id, name, email, createdAt } = session.user;
  return { id, name, email, createdAt, sources: userSources(session.user) };
});

/** The signed-in user, or a redirect to the log-in page. */
export async function requireUser(next?: string): Promise<CurrentUser> {
  const user = await getCurrentUser();
  if (!user) redirect(withNext("/login", next));
  return user;
}

/** Like requireUser, and sends users who haven't picked sources yet to onboarding. */
export async function requireOnboardedUser(next?: string): Promise<CurrentUser> {
  const user = await requireUser(next);
  if (user.sources.length === 0) redirect("/onboarding");
  return user;
}

export interface CurrentOrganizations {
  memberships: Membership[];
  /** The organization this session works in (lib/organizations.ts activeMembership). */
  active: Membership | null;
}

/** The signed-in user's organizations (EMBER-124), their personal workspace first. Empty when signed out. */
export const getCurrentOrganizations = cache(async (): Promise<CurrentOrganizations> => {
  const session = await getSession();
  if (!session) return { memberships: [], active: null };
  const { adapter } = await getAuth().$context;
  const memberships = await listMemberships(adapter as unknown as OrgAdapter, session.user.id);
  return { memberships, active: activeMembership(memberships, activeOrganizationId(session.session)) };
});

/**
 * Like requireOnboardedUser, with the organization the session works in.
 * Every page in the signed-in app works in one: at least the user's personal
 * workspace (EMBER-165).
 */
export async function requireOrganization(next?: string): Promise<CurrentUser & { organization: Membership; memberships: Membership[] }> {
  const user = await requireOnboardedUser(next);
  const { memberships, active } = await getCurrentOrganizations();
  if (!active) throw new Error(`user ${user.id} has no personal workspace`);
  return { ...user, organization: active, memberships };
}

/** Only same-site paths are allowed after log-in, never another origin. */
export function safeNext(next: string | string[] | undefined, fallback = "/dashboard"): string {
  const value = Array.isArray(next) ? next[0] : next;
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return fallback;
  return value;
}

/** `path` with `?next=` when `next` is a same-site path, else plain `path` (log-in and sign-up pass it to each other). */
export function withNext(path: string, next: string | string[] | undefined): string {
  const target = safeNext(next, "");
  return target ? `${path}?next=${encodeURIComponent(target)}` : path;
}
