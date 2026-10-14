// Server-side session lookups for pages and layouts. Each request reads the
// session once (React cache), however many components ask for it.

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";
import { getAuth } from "./auth.ts";
import { userSources, type SourceId } from "./sources.ts";

export interface CurrentUser {
  id: string;
  name: string;
  email: string;
  sources: SourceId[];
  createdAt: Date;
}

export const getCurrentUser = cache(async (): Promise<CurrentUser | null> => {
  // Read the request first: it marks the page dynamic, so the build never
  // creates the auth instance (which needs the runtime secret) to prerender.
  const requestHeaders = await headers();
  const session = await getAuth().api.getSession({ headers: requestHeaders });
  if (!session) return null;
  const { id, name, email, createdAt } = session.user;
  return { id, name, email, createdAt, sources: userSources(session.user) };
});

/** The signed-in user, or a redirect to the log-in page. */
export async function requireUser(next?: string): Promise<CurrentUser> {
  const user = await getCurrentUser();
  if (!user) redirect(next ? `/login?next=${encodeURIComponent(next)}` : "/login");
  return user;
}

/** Like requireUser, and sends users who haven't picked sources yet to onboarding. */
export async function requireOnboardedUser(next?: string): Promise<CurrentUser> {
  const user = await requireUser(next);
  if (user.sources.length === 0) redirect("/onboarding");
  return user;
}

/** Only same-site paths are allowed after log-in, never another origin. */
export function safeNext(next: string | string[] | undefined, fallback = "/dashboard"): string {
  const value = Array.isArray(next) ? next[0] : next;
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return fallback;
  return value;
}
