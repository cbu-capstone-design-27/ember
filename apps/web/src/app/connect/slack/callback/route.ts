// Connect Slack, step 2: the Slack app's Redirect URL (EMBER-148, ADR 0010).
// Slack sends the browser here after the install screen, with `code` and our
// `state`, or with `error` when the person cancelled.
//
// The code becomes the workspace's bot token. `oauth.v2.access` names the
// workspace, and only someone Slack lets install apps there gets a code, which
// is the proof of ownership. The mapping and the token are written together,
// then the deliveries intake stored for the workspace go to enrich.

import { after, NextResponse, type NextRequest } from "next/server";
import { handOffAgainLater } from "../../../../lib/connections.ts";
import { connectWorkspace, matchState, missingScopes, SlackClient, slackAppConfig, SlackError, SOURCE, STATE_COOKIE } from "../../../../lib/slack-connect.ts";
import { apiKeyRing, appOrigin, connector, cookieSecret, ingestionPool } from "../../server.ts";
import { callbackUrl, clearCookie, settingsUrl } from "../server.ts";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const done = (notice: string, account?: string) => {
    const response = NextResponse.redirect(settingsUrl(request, notice, account));
    clearCookie(response, STATE_COOKIE);
    return response;
  };

  // access_denied: the person cancelled on Slack's install screen
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
  const config = slackAppConfig();
  const pool = ingestionPool();
  const ring = apiKeyRing();
  if (!config || !pool || !ring) return done("unavailable");
  const code = params.get("code");
  if (!code) return done("error");

  const client = new SlackClient(config);
  const { membership } = who.value;
  try {
    const install = await client.exchangeCode(code, callbackUrl(request));
    const missing = missingScopes(install.scope);
    if (missing.length > 0) {
      // The app's configured scopes don't match the manifest's. Not uninstalled here: the workspace
      // may already be connected, to this organization or another, on this same app
      console.warn(`[connect] Slack ${install.teamId} granted without ${missing.join(", ")}`);
      return done("scopes");
    }
    const result = await connectWorkspace(pool, ring, { install, tenant: membership.tenant, userId: who.value.userId });
    if (!result.ok) {
      // The app stays installed for the tenant that holds the workspace; its token there is unchanged
      return done("other-tenant");
    }
    after(() => handOffAgainLater(pool, { source: SOURCE, accountId: install.teamId, tenant: membership.tenant }));
    return done(result.reconnected ? "reconnected" : "connected", install.teamId);
  } catch (error) {
    if (error instanceof SlackError && error.code === "enterprise_install") return done("enterprise");
    console.warn(`[connect] Slack callback for organization ${membership.id} failed: ${(error as Error).message}`);
    return done("error");
  }
}
