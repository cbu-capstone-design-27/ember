// Connect Jira, step 2: the 3LO app's callback URL (EMBER-131, ADR 0010).
// Atlassian sends the browser here after consent, with `code` and our
// `state`, or with `error` when the person declined.
//
// The code becomes a token pair, and accessible-resources lists the Jira
// sites it covers: only sites the consenting user can use, which is the proof
// of ownership. The tokens wait server side, sealed, while the owner picks a
// site and its projects (../../(app)/connect/jira/pick).

import { NextResponse, type NextRequest } from "next/server";
import { heldAccounts } from "../../../../lib/connections.ts";
import {
  AtlassianClient,
  jiraAppConfig,
  matchState,
  PICK_COOKIE,
  PICK_TTL_SECONDS,
  savePending,
  seal,
  SCOPES,
  STATE_COOKIE,
  type PendingSite,
  type PickState,
} from "../../../../lib/jira-connect.ts";
import { appOrigin, connector, cookieSecret, ingestionPool } from "../../server.ts";
import { apiKeyRing, callbackUrl, clearCookie, cookieOptions, settingsUrl, verificationStore } from "../server.ts";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const done = (notice: string) => {
    const response = NextResponse.redirect(settingsUrl(request, notice));
    clearCookie(response, STATE_COOKIE);
    return response;
  };

  // access_denied: the person declined on the consent screen
  if (params.get("error")) return done("cancelled");

  const session = await connector(request.headers);
  if (!session.ok && session.reason === "signed-out") {
    return NextResponse.redirect(new URL("/login?next=/settings", appOrigin(request)));
  }
  const userId = session.ok ? session.value.userId : null;
  const saved = userId ? matchState(request.cookies.get(STATE_COOKIE)?.value, params.get("state"), userId, await cookieSecret()) : null;
  if (!saved) return done("expired");

  // Check the role again: it may have changed during the round trip
  const who = await connector(request.headers, saved.organizationId);
  if (!who.ok) return done("forbidden");
  const config = jiraAppConfig();
  const pool = ingestionPool();
  const ring = apiKeyRing();
  if (!config || !pool || !ring) return done("unavailable");
  const code = params.get("code");
  if (!code) return done("error");

  const client = new AtlassianClient(config);
  const { membership } = who.value;
  try {
    const tokens = await client.exchangeCode(code, callbackUrl(request));
    const granted = new Set(tokens.scope.split(" "));
    // Atlassian may grant less than asked when the app's console scopes don't match SCOPES
    if (SCOPES.some((scope) => scope !== "offline_access" && !granted.has(scope))) return done("scopes");

    const sites = await client.sites(tokens.accessToken);
    if (sites.length === 0) return done("no-sites");
    const held = await heldAccounts(pool, "jira", sites.map((s) => ({ accountId: s.host, externalId: s.cloudId })));
    const pending: PendingSite[] = [];
    for (const site of sites) {
      const holder = held.get(site.host);
      if (holder && holder.tenant !== membership.tenant) {
        pending.push({ ...site, projects: [], more: false, blocked: "Connected to another Ember organization." });
        continue;
      }
      try {
        const { projects, more } = await client.projects(tokens.accessToken, site.cloudId);
        pending.push({ ...site, projects, more, blocked: projects.length === 0 ? "You can't see any project on this site." : null });
      } catch (error) {
        console.warn(`[connect] Jira projects of ${site.host} for organization ${membership.id}: ${(error as Error).message}`);
        pending.push({ ...site, projects: [], more: false, blocked: "Jira didn't list this site's projects. Try again." });
      }
    }
    // Only shown on the picker and kept with the credential, so a failure here doesn't stop the connect
    const atlassian = await client.myself(tokens.accessToken, sites[0].cloudId).catch(() => ({ accountId: "", displayName: "" }));
    const pendingId = await savePending(await verificationStore(), ring, {
      organizationId: membership.id,
      userId: who.value.userId,
      tokens,
      atlassian,
      sites: pending,
    });

    const pick: PickState = { pendingId, organizationId: membership.id, userId: who.value.userId };
    const response = NextResponse.redirect(new URL("/connect/jira/pick", appOrigin(request)));
    clearCookie(response, STATE_COOKIE);
    response.cookies.set(PICK_COOKIE, seal(pick, await cookieSecret(), PICK_TTL_SECONDS), cookieOptions(request, PICK_TTL_SECONDS));
    return response;
  } catch (error) {
    console.warn(`[connect] Jira callback for organization ${membership.id} failed: ${(error as Error).message}`);
    return done("error");
  }
}
