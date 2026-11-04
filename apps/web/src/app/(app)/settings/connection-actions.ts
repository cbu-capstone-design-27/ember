"use server";

// Disconnect (EMBER-147, ADR 0010): an owner or admin stops ingesting one of
// the organization's connected accounts. The data stays (EMBER-53). The tenant
// comes from the caller's membership, never from the form, so a forged form
// can only name an account of their own organization.

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { disconnectAccount } from "../../../lib/connections.ts";
import { connector, ingestionPool } from "../../connect/github/server.ts";

function settings(query: Record<string, string>): never {
  redirect(`/settings?${new URLSearchParams(query)}#connections`);
}

export async function disconnectConnection(formData: FormData) {
  const organizationId = formData.get("organizationId");
  const source = formData.get("source");
  const accountId = formData.get("accountId");
  // A server action is a public endpoint: check what the browser sent.
  if (typeof organizationId !== "string" || typeof source !== "string" || typeof accountId !== "string" || !organizationId || !source || !accountId) {
    settings({ disconnect: "error" });
  }
  const who = await connector(await headers(), organizationId);
  if (!who.ok) settings({ disconnect: "forbidden" });
  const pool = ingestionPool();
  if (!pool) settings({ disconnect: "unavailable" });

  const result = await disconnectAccount(pool, { source, accountId, tenant: who.value.membership.tenant });
  if (!result.ok) settings({ disconnect: "not-found" });
  // Removing the source's own hook needs a credential per source: Jira deletes its dynamic webhook
  // (EMBER-131), Slack calls apps.uninstall (EMBER-148). GitHub's needs the App's private key, which
  // the web app doesn't hold, so Settings sends the owner to GitHub to uninstall (ADR 0010).
  console.log(`[connect] ${who.value.userId} disconnected ${source} ${accountId} from ${who.value.membership.tenant}`);
  settings({ disconnect: "done", source, account: accountId });
}
