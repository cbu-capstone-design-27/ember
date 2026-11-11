"use server";

// Connect GitLab (EMBER-149, ADR 0010): check the pasted instance, group and
// token, and map the group to the organization; then take the signing token
// GitLab generated for each webhook the owner adds. The tenant comes from the
// caller's membership, never from the form, and no token is logged.

import { headers } from "next/headers";
import { after } from "next/server";
import { handOffAgainLater } from "../../../../lib/connections.ts";
import { addSigningToken, connectGroup, gitlabConnectConfig, SOURCE, type TokenKind } from "../../../../lib/gitlab-connect.ts";
import { connector, ingestionPool, keyRing } from "../../../connect/server.ts";

export type ConnectGitLabState =
  | { status: "idle" }
  | { status: "error"; text: string; instance: string; group: string }
  | { status: "connected"; accountId: string; label: string; tokenKind: TokenKind; expiresAt: string | null; reconnected: boolean };

export type SigningTokenState = { status: "idle" } | { status: "error"; text: string } | { status: "added"; count: number; already: boolean };

const ERRORS: Record<string, string> = {
  "invalid-instance": "Enter the instance's https address, like https://gitlab.com or https://gitlab.example.com.",
  "invalid-group": "Enter the top-level group's path, like acme, not a subgroup or project.",
  "no-token": "Paste a read_api access token.",
  "invalid-token": "GitLab refused that token. It may be mistyped, revoked or expired.",
  scope: "That token doesn't have the read_api scope.",
  "write-scope": "That token can write (it has the api scope or another write scope). Ember stores only read-only tokens: make one with read_api alone.",
  "not-found": "That token can't see the group, or there's no group at that path on this instance.",
  "not-top-level": "That's a subgroup. Connect its top-level group: Ember files a whole top-level group under one organization.",
  "not-member": "The token's user isn't a member of that group. Anyone can read a public group, so that doesn't show it's yours.",
  unreachable: "The GitLab instance didn't answer as expected. Check the address and try again.",
  "other-tenant": "That group is connected to another Ember organization. A group can't move between organizations.",
  forbidden: "Only owners and admins of this organization can connect sources.",
  unavailable: "Connecting GitLab isn't set up on this server.",
};

const TOKEN_ERRORS: Record<string, string> = {
  "invalid-signing-token": "That isn't a whole signing token. GitLab shows it as whsec_ followed by 44 characters.",
  "not-found": "That group isn't connected to this organization any more.",
  limit: "That group has as many webhooks as Ember keeps. Disconnect and reconnect it to start over.",
  forbidden: ERRORS.forbidden,
  unavailable: ERRORS.unavailable,
};

function field(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

/** Who may connect, and the tenant: from the session and their membership in the form's organization. */
async function caller(formData: FormData) {
  const organizationId = field(formData, "organizationId");
  if (!organizationId) return null;
  const who = await connector(await headers(), organizationId);
  return who.ok ? who.value : null;
}

export async function connectGitLab(_previous: ConnectGitLabState, formData: FormData): Promise<ConnectGitLabState> {
  const instance = field(formData, "instance");
  const group = field(formData, "group");
  const fail = (reason: string): ConnectGitLabState => ({ status: "error", text: ERRORS[reason] ?? ERRORS.unreachable, instance, group });

  const who = await caller(formData);
  if (!who) return fail("forbidden");
  const pool = ingestionPool();
  const api = keyRing("api");
  const config = gitlabConnectConfig();
  // The webhook ring too: the next step stores the signing tokens with it
  if (!pool || !api || !keyRing("webhook") || !config) return fail("unavailable");

  const tenant = who.membership.tenant;
  const result = await connectGroup(pool, api, config, { instance, group, token: field(formData, "token"), tenant, userId: who.userId });
  if (!result.ok) return fail(result.reason);

  after(() => handOffAgainLater(pool, { source: SOURCE, accountId: result.accountId, tenant }));
  console.log(`[connect] ${who.userId} connected gitlab ${result.accountId} to ${tenant} (${result.tokenKind} access token)`);
  return {
    status: "connected",
    accountId: result.accountId,
    label: result.label,
    tokenKind: result.tokenKind,
    expiresAt: result.expiresAt,
    reconnected: result.reconnected,
  };
}

export async function addWebhookSigningToken(_previous: SigningTokenState, formData: FormData): Promise<SigningTokenState> {
  const fail = (reason: string): SigningTokenState => ({ status: "error", text: TOKEN_ERRORS[reason] ?? TOKEN_ERRORS["not-found"] });
  const who = await caller(formData);
  if (!who) return fail("forbidden");
  const pool = ingestionPool();
  const webhook = keyRing("webhook");
  if (!pool || !webhook) return fail("unavailable");
  const accountId = field(formData, "accountId");
  const tenant = who.membership.tenant;
  const result = await addSigningToken(pool, webhook, { accountId, tenant, token: field(formData, "signingToken") });
  if (!result.ok) return fail(result.reason);
  if (!result.already) console.log(`[connect] ${who.userId} added a webhook signing token to gitlab ${accountId} (${result.count} now)`);
  return { status: "added", count: result.count, already: result.already };
}
