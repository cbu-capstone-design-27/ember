// Connect Slack, step 1 (EMBER-148, ADR 0010): send an owner of the active
// organization to Slack's OAuth v2 install screen for Ember's app. Slack
// comes back to ./callback with our state, which a signed cookie ties to this
// browser, user and organization.

import { NextResponse, type NextRequest } from "next/server";
import { authorizeUrl, newState, seal, slackAppConfig, STATE_COOKIE, STATE_TTL_SECONDS, type ConnectState } from "../../../lib/slack-connect.ts";
import { apiKeyRing, connector, cookieSecret, ingestionPool, onMainAddress, refuseConnector } from "../server.ts";
import { callbackUrl, cookieOptions, settingsUrl } from "./server.ts";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const main = onMainAddress(request);
  if (main) return NextResponse.redirect(main);

  const who = await connector(request.headers);
  if (!who.ok) return refuseConnector(request, who.reason, settingsUrl(request, "forbidden"));
  const config = slackAppConfig();
  if (!config || !ingestionPool() || !apiKeyRing()) return NextResponse.redirect(settingsUrl(request, "unavailable"));

  const state = newState();
  const saved: ConnectState = { state, organizationId: who.value.membership.id, userId: who.value.userId };
  const response = NextResponse.redirect(authorizeUrl(config, state, callbackUrl(request)));
  response.cookies.set(STATE_COOKIE, seal(saved, await cookieSecret(), STATE_TTL_SECONDS), cookieOptions(request, STATE_TTL_SECONDS));
  return response;
}
