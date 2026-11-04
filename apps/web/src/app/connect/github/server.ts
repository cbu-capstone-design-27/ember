// What the Connect GitHub routes share (EMBER-130): the app's address, the
// signed-in member who may connect sources, the Postgres pool, and the
// cookie settings. Server only.

import { NextResponse, type NextRequest } from "next/server";
import type pg from "pg";
import { activeOrganizationId, getAuth } from "../../../lib/auth.ts";
import { isPostgresDatabase } from "../../../lib/db.ts";
import { CALLBACK_PATH, COOKIE_PATH, type Candidate } from "../../../lib/github-connect.ts";
import { activeMembership, canConnectSources, listMemberships, type Membership, type OrgAdapter } from "../../../lib/organizations.ts";

/**
 * The address GitHub sends people back to. BETTER_AUTH_URL on the cluster,
 * where the request's own URL is the pod's; the request's origin in dev.
 */
export function appOrigin(request: NextRequest): string {
  return process.env.BETTER_AUTH_URL ? new URL(process.env.BETTER_AUTH_URL).origin : request.nextUrl.origin;
}

export function callbackUrl(request: NextRequest): string {
  return new URL(CALLBACK_PATH, appOrigin(request)).toString();
}

/**
 * The same path on BETTER_AUTH_URL, for a request that came in on one of the
 * other trusted addresses. The App has one Callback URL and the session cookie
 * belongs to one host, so the whole round trip runs on the main address. Null
 * when the request is on it already, or on an address we don't know.
 */
export function onMainAddress(request: NextRequest): URL | null {
  if (!process.env.BETTER_AUTH_URL) return null;
  const main = new URL(process.env.BETTER_AUTH_URL);
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  if (!host || host === main.host) return null;
  const others = (process.env.BETTER_AUTH_TRUSTED_ORIGINS ?? "").split(",").flatMap((origin) => {
    try {
      return [new URL(origin.trim()).host];
    } catch {
      return [];
    }
  });
  if (!others.includes(host)) return null;
  return new URL(request.nextUrl.pathname + request.nextUrl.search, main.origin);
}

/** Settings, scrolled to Connections, with a notice (settings/page.tsx GITHUB_NOTICES). */
export function settingsUrl(request: NextRequest, notice: string, account?: string): URL {
  const url = new URL("/settings", appOrigin(request));
  url.searchParams.set("github", notice);
  if (account) url.searchParams.set("account", account);
  url.hash = "connections";
  return url;
}

/** The ingestion database, or null on SQLite, which has no `installations`. */
export function ingestionPool(): pg.Pool | null {
  const { database } = getAuth().options;
  return isPostgresDatabase(database) ? database.pool : null;
}

/** The key the state and picker cookies are signed with: Better Auth's secret. */
export async function cookieSecret(): Promise<string> {
  return (await getAuth().$context).secret;
}

export function cookieOptions(request: NextRequest, maxAge: number) {
  return {
    httpOnly: true,
    // GitHub's redirect back is a top-level GET from another site: Lax still sends the cookie
    sameSite: "lax" as const,
    secure: appOrigin(request).startsWith("https:"),
    path: COOKIE_PATH,
    maxAge,
  };
}

export function clearCookie(response: NextResponse, name: string) {
  response.cookies.set(name, "", { path: COOKIE_PATH, maxAge: 0 });
}

export interface Connector {
  userId: string;
  membership: Membership;
}

/**
 * The signed-in user and their membership in `organizationId` (their active
 * organization when it's not given), or why they can't connect a source.
 */
export async function connector(
  requestHeaders: Headers,
  organizationId?: string,
): Promise<{ ok: true; value: Connector } | { ok: false; reason: "signed-out" | "no-organization" | "forbidden" }> {
  const auth = getAuth();
  const session = await auth.api.getSession({ headers: requestHeaders });
  if (!session) return { ok: false, reason: "signed-out" };
  const { adapter } = await auth.$context;
  const memberships = await listMemberships(adapter as unknown as OrgAdapter, session.user.id);
  const membership = organizationId
    ? memberships.find((m) => m.id === organizationId)
    : activeMembership(memberships, activeOrganizationId(session.session));
  if (!membership) return { ok: false, reason: organizationId ? "forbidden" : "no-organization" };
  if (!canConnectSources(membership.role)) return { ok: false, reason: "forbidden" };
  return { ok: true, value: { userId: session.user.id, membership } };
}

/** The existing-installation picker's cookie (PICK_COOKIE): what the callback found, checked against GitHub. */
export interface PickState {
  organizationId: string;
  userId: string;
  candidates: Candidate[];
}
