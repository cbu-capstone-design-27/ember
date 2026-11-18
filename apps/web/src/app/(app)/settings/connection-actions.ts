"use server";

// Disconnect (EMBER-147, ADR 0010): an owner or admin stops ingesting one of
// the organization's connected accounts. Stored data isn't deleted yet (EMBER-153). The tenant
// comes from the caller's membership, never from the form, so a forged form
// can only name an account of their own organization.

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { disconnectAccount, listConnections } from "../../../lib/connections.ts";
import { AtlassianClient, jiraAppConfig, removeWebhooks } from "../../../lib/jira-connect.ts";
import { SlackClient, slackAppConfig, uninstallWorkspace } from "../../../lib/slack-connect.ts";
import { apiKeyRing, connector, ingestionPool } from "../../connect/server.ts";

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

  const tenant = who.value.membership.tenant;

  // Remove the source's own hook while its credential still exists (ADR 0010, Disconnect step 3).
  // Jira deletes its dynamic webhooks (EMBER-131); Slack uninstalls the app with apps.uninstall
  // (EMBER-148). GitHub's needs the App's private key, which the web app doesn't hold, so Settings
  // sends the owner to GitHub to uninstall.
  let hook: "deleted" | "failed" | "none" = "none";
  if (source === "jira" || source === "slack") {
    const ours = (await listConnections(pool, tenant, source)).some((c) => c.accountId === accountId && c.disconnectedAt === null);
    const ring = apiKeyRing();
    if (ours && source === "jira") {
      const config = jiraAppConfig();
      hook = config && ring ? await removeWebhooks(pool, ring, new AtlassianClient(config), accountId) : "failed";
    } else if (ours) {
      const config = slackAppConfig();
      hook = config && ring ? await uninstallWorkspace(pool, ring, new SlackClient(config), accountId) : "failed";
    }
  }

  const result = await disconnectAccount(pool, { source, accountId, tenant });
  if (!result.ok) settings({ disconnect: "not-found" });
  console.log(`[connect] ${who.value.userId} disconnected ${source} ${accountId} from ${tenant} (source hook: ${hook})`);
  settings({ disconnect: "done", source, account: accountId, ...(source === "jira" || source === "slack" ? { hook } : {}) });
}
