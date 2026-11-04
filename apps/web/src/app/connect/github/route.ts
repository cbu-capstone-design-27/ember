// Connect GitHub, step 1 (EMBER-130, ADR 0010): send an owner of the active
// organization to GitHub. `/connect/github` installs the App; with
// `?existing=1` it signs them in to GitHub instead, to pick an installation
// that already exists. Either way GitHub comes back to ./callback with our
// state, which a signed cookie ties to this browser, user and organization.

import { NextResponse, type NextRequest } from "next/server";
import {
  authorizeUrl,
  githubAppConfig,
  installUrl,
  newState,
  seal,
  STATE_COOKIE,
  STATE_TTL_SECONDS,
  type ConnectState,
} from "../../../lib/github-connect.ts";
import { appOrigin, callbackUrl, connector, cookieOptions, cookieSecret, ingestionPool, onMainAddress, settingsUrl } from "./server.ts";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const main = onMainAddress(request);
  if (main) return NextResponse.redirect(main);

  const who = await connector(request.headers);
  if (!who.ok) {
    // Back to Settings, not here: log-in follows `next` with a client-side navigation, which can't
    // follow this route's redirect to github.com
    if (who.reason === "signed-out") return NextResponse.redirect(new URL("/login?next=%2Fsettings", appOrigin(request)));
    if (who.reason === "no-organization") return NextResponse.redirect(new URL("/organizations", appOrigin(request)));
    return NextResponse.redirect(settingsUrl(request, "forbidden"));
  }
  const config = githubAppConfig();
  if (!config || !ingestionPool()) return NextResponse.redirect(settingsUrl(request, "unavailable"));

  const mode = request.nextUrl.searchParams.has("existing") ? "existing" : "install";
  const state = newState();
  const target = mode === "install" ? installUrl(config, state) : authorizeUrl(config, state, callbackUrl(request));
  const saved: ConnectState = { state, organizationId: who.value.membership.id, userId: who.value.userId, mode };
  const response = NextResponse.redirect(target);
  response.cookies.set(STATE_COOKIE, seal(saved, await cookieSecret(), STATE_TTL_SECONDS), cookieOptions(request, STATE_TTL_SECONDS));
  return response;
}
