"use server";

// Connect Jira, step 4 (EMBER-131, ADR 0010): connect the site the owner
// picked, with the projects they ticked. Only a site and projects the
// callback found for this consent are accepted: the browser sends ids, and
// the pending consent, sealed server side, is what they are checked against.

import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { after } from "next/server";
import { handOffAgainLater } from "../../../../../lib/connections.ts";
import {
  AtlassianClient,
  AtlassianError,
  connectSite,
  COOKIE_PATH,
  deletePending,
  jiraAppConfig,
  loadPending,
  PICK_COOKIE,
  unseal,
  type PickState,
} from "../../../../../lib/jira-connect.ts";
import { connector, cookieSecret, ingestionPool } from "../../../../connect/server.ts";
import { apiKeyRing, verificationStore } from "../../../../connect/jira/server.ts";

function settings(notice: string, account?: string): never {
  const query = new URLSearchParams({ jira: notice });
  if (account) query.set("account", account);
  redirect(`/settings?${query}#connections`);
}

export async function connectPickedSite(formData: FormData) {
  const jar = await cookies();
  const pick = unseal<PickState>(jar.get(PICK_COOKIE)?.value, await cookieSecret());
  if (!pick) settings("expired");
  const who = await connector(await headers(), pick.organizationId);
  if (!who.ok || who.value.userId !== pick.userId) settings("forbidden");
  const pool = ingestionPool();
  const ring = apiKeyRing();
  const config = jiraAppConfig();
  if (!pool || !ring || !config) settings("unavailable");

  const store = await verificationStore();
  const consent = await loadPending(store, ring, pick);
  if (!consent) settings("expired");
  // The access token lasts an hour and the consent fifteen minutes, so it is still good
  const site = consent.sites.find((s) => s.cloudId === formData.get("site") && s.blocked === null);
  if (!site) settings("error");

  const tenant = who.value.membership.tenant;
  let notice: { key: string; account?: string };
  try {
    const result = await connectSite(pool, ring, new AtlassianClient(config), {
      consent,
      site,
      projectIds: formData.getAll("project").map(String),
      tenant,
      userId: who.value.userId,
    });
    if (result.ok) {
      after(() => handOffAgainLater(pool, { source: "jira", accountId: result.host, tenant }));
      console.log(`[connect] ${who.value.userId} connected jira ${result.host} to ${tenant}`);
      notice = { key: "connected", account: result.host };
    } else {
      notice = { key: result.reason, account: site.host };
    }
  } catch (error) {
    console.warn(`[connect] Jira connect of ${site.host} for ${tenant} failed: ${(error as Error).message}`);
    // The 5-per-user limit is the one an owner can fix themselves
    const limit = error instanceof AtlassianError && /limit/i.test(error.message);
    notice = { key: limit ? "limit" : "error", account: site.host };
  }
  // Back to the picker, consent kept, to tick some
  if (notice.key === "no-projects") redirect(`/connect/jira/pick?none=${encodeURIComponent(site.cloudId)}`);
  await deletePending(store, pick.pendingId);
  jar.set(PICK_COOKIE, "", { path: COOKIE_PATH, maxAge: 0 });
  settings(notice.key, notice.account);
}
