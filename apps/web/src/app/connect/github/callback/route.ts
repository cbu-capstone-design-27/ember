// Connect GitHub, step 2: the App's Callback URL (EMBER-130, ADR 0010).
// GitHub sends the browser here after an install, with `code`,
// `installation_id` and `setup_action`, or after the plain OAuth sign-in of
// the existing-installation path, with `code` only. Both carry our `state`.
//
// The installation is accepted only if GitHub lists it for the user the
// `code` belongs to. `installation_id` alone proves nothing: anyone can type
// one into this URL.

import { after, NextResponse, type NextRequest } from "next/server";
import { connectAccount, handOffAgainLater, heldAccounts } from "../../../../lib/connections.ts";
import {
  candidateInstallations,
  GitHubClient,
  githubAppConfig,
  matchState,
  PICK_COOKIE,
  PICK_TTL_SECONDS,
  seal,
  STATE_COOKIE,
  verifyInstallation,
  withUserToken,
  type Candidate,
} from "../../../../lib/github-connect.ts";
import {
  appOrigin,
  callbackUrl,
  clearCookie,
  connector,
  cookieOptions,
  cookieSecret,
  ingestionPool,
  settingsUrl,
  type PickState,
} from "../server.ts";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const done = (notice: string, account?: string) => {
    const response = NextResponse.redirect(settingsUrl(request, notice, account));
    clearCookie(response, STATE_COOKIE);
    return response;
  };

  // An organization member asked an owner to approve the install. There is no installation yet
  if (params.get("setup_action") === "request") return done("requested");
  // The person declined on GitHub's sign-in page
  if (params.get("error")) return done("cancelled");

  const session = await connector(request.headers);
  if (!session.ok && session.reason === "signed-out") {
    return NextResponse.redirect(new URL("/login?next=/settings", appOrigin(request)));
  }
  const userId = session.ok ? session.value.userId : null;
  const saved = userId ? matchState(request.cookies.get(STATE_COOKIE)?.value, params.get("state"), userId, await cookieSecret()) : null;
  if (!saved) {
    // An install started on GitHub's own page comes back without our state, so we can't tell which
    // organization it is for. The existing-installation path picks it up
    return done(params.get("installation_id") ? "installed" : "expired");
  }

  // Check the role again: it may have changed during the round trip
  const who = await connector(request.headers, saved.organizationId);
  if (!who.ok) return done("forbidden");
  const config = githubAppConfig();
  const pool = ingestionPool();
  if (!config || !pool) return done("unavailable");
  const code = params.get("code");
  if (!code) return done("error");

  const client = new GitHubClient(config);
  const { membership } = who.value;
  try {
    if (saved.mode === "install") {
      const installation = await withUserToken(client, code, callbackUrl(request), (token) =>
        verifyInstallation(client, token, params.get("installation_id") ?? ""),
      );
      if (!installation) return done("not-yours");
      const input = {
        source: "github",
        accountId: String(installation.id),
        tenant: membership.tenant,
        label: installation.account.login,
        externalId: String(installation.account.id),
        userId: who.value.userId,
      };
      const result = await connectAccount(pool, input);
      if (!result.ok) return done("other-tenant", installation.account.login);
      after(() => handOffAgainLater(pool, input));
      return done("connected", installation.account.login);
    }

    const found = await withUserToken(client, code, callbackUrl(request), (token) => candidateInstallations(client, token));
    const held = await heldAccounts(
      pool,
      "github",
      found.map((c) => ({ accountId: c.installationId, externalId: c.accountId })),
    );
    const candidates = found.map((c): Candidate => {
      const holder = held.get(c.installationId);
      if (holder && holder.tenant !== membership.tenant) return { ...c, blocked: "Connected to another Ember organization." };
      if (holder?.connected) return { ...c, blocked: "Already connected." };
      return c;
    });
    const pick: PickState = { organizationId: membership.id, userId: who.value.userId, candidates };
    const response = NextResponse.redirect(new URL("/connect/github/pick", appOrigin(request)));
    clearCookie(response, STATE_COOKIE);
    response.cookies.set(PICK_COOKIE, seal(pick, await cookieSecret(), PICK_TTL_SECONDS), cookieOptions(request, PICK_TTL_SECONDS));
    return response;
  } catch (error) {
    console.warn(`[connect] GitHub callback for organization ${membership.id} failed: ${(error as Error).message}`);
    return done("error");
  }
}
