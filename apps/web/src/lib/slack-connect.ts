// Connect Slack (EMBER-148, ADR 0010): an organization's owner installs
// Ember's Slack app into a workspace through Slack's OAuth v2 install, and the
// web app:
//
// 1. maps the workspace's `team_id` to the organization's tenant in `installations`;
// 2. stores the bot token as the workspace's `api` credential (`source_credentials`),
//    in the same transaction, so enrich never sees the mapping without its token;
// 3. hands the deliveries intake stored for the workspace to enrich.
//
// Slack proves the workspace is theirs: only someone allowed to install apps
// there gets through the consent screen, and `oauth.v2.access` names the
// workspace the token belongs to. No id the browser sends is trusted.
//
// Server only: it holds the app's client secret.

import { randomBytes } from "node:crypto";
import type pg from "pg";
import { connectAccount, type ConnectResult } from "./connections.ts";
import { cookieSealer, sameState } from "./signed-cookies.ts";
import { KeyRing, loadCredential } from "./source-credentials.ts";

export const SLACK_WEB = "https://slack.com";
export const SLACK_API = "https://slack.com/api";
export const SOURCE = "slack";

/** Where Slack sends the browser back: the app's Redirect URL is this path on BETTER_AUTH_URL. */
export const CALLBACK_PATH = "/connect/slack/callback";
/** The state cookie is sent only to the connect routes. */
export const COOKIE_PATH = "/connect/slack";
/** The round trip through Slack: which organization, who started it, and the state Slack must echo. */
export const STATE_COOKIE = "ember_slack_connect";
export const STATE_TTL_SECONDS = 15 * 60;

/**
 * The bot scopes, as services/slack-ingestion/slack-app-manifest.json and
 * receiver.REQUIRED_BOT_SCOPES list them (docs/ingestion/slack.md, Auth).
 * All read-only; no user scopes.
 */
export const BOT_SCOPES = [
  "channels:history",
  "channels:read",
  "groups:history",
  "groups:read",
  "users:read",
  "users:read.email",
  "team:read",
  "reactions:read",
] as const;

export interface SlackAppConfig {
  clientId: string;
  clientSecret: string;
}

/** The Slack app's OAuth client, or null while either half is missing (Connect Slack is then unavailable). */
export function slackAppConfig(env: Record<string, string | undefined> = process.env): SlackAppConfig | null {
  const clientId = env.SLACK_CLIENT_ID?.trim();
  const clientSecret = env.SLACK_CLIENT_SECRET?.trim();
  return clientId && clientSecret ? { clientId, clientSecret } : null;
}

// The state cookie is signed with a key only this flow uses.
export const { seal, unseal } = cookieSealer("ember:connect-slack");

/** The state cookie's content. `state` is what Slack has to send back. */
export interface ConnectState {
  state: string;
  organizationId: string;
  userId: string;
}

export function newState(): string {
  return randomBytes(24).toString("base64url");
}

/** The state cookie's content when the callback belongs to the round trip this browser started, else null. */
export function matchState(sealed: string | undefined, returned: string | null, userId: string, secret: string, now = Date.now()) {
  const value = unseal<ConnectState>(sealed, secret, now);
  if (!value || value.userId !== userId) return null;
  return sameState(value.state, returned) ? value : null;
}

/** Slack's install screen for the bot scopes. The workspace is the one the person picks there. */
export function authorizeUrl(config: SlackAppConfig, state: string, redirectUri: string): string {
  const query = new URLSearchParams({
    client_id: config.clientId,
    scope: BOT_SCOPES.join(","),
    redirect_uri: redirectUri,
    state,
  });
  return `${SLACK_WEB}/oauth/v2/authorize?${query}`;
}

/** A Slack workspace id, as intake reads it from a delivery's `team_id`. */
export function isTeamId(value: unknown): value is string {
  return typeof value === "string" && /^T[A-Z0-9]{2,30}$/.test(value);
}

// ---------------------------------------------------------------------------
// Slack's API.

export class SlackError extends Error {
  /** Slack's `error` code, e.g. invalid_code, or the HTTP status when there was none. */
  readonly code: string;

  constructor(message: string, code: string) {
    super(message);
    this.code = code;
  }
}

/** What a finished install gives: the workspace, and the bot token that reads it. */
export interface Install {
  teamId: string;
  teamName: string;
  botToken: string;
  botUserId: string;
  appId: string;
  scope: string;
  /** The Slack user who installed it. */
  installerId: string;
  /** The Enterprise Grid organization the workspace is part of, if any. */
  enterpriseId: string | null;
}

type Fetch = typeof fetch;

export class SlackClient {
  private readonly config: SlackAppConfig;
  private readonly fetchImpl: Fetch;

  constructor(config: SlackAppConfig, fetchImpl: Fetch = fetch) {
    this.config = config;
    this.fetchImpl = fetchImpl;
  }

  /** POST a Web API method, form-encoded, and return its body when `ok`. */
  private async call<T>(method: string, params: Record<string, string>): Promise<T & { ok: true }> {
    const response = await this.fetchImpl(`${SLACK_API}/${method}`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(params).toString(),
      signal: AbortSignal.timeout(10_000),
    });
    const body = (await response.json().catch(() => null)) as ({ ok?: boolean; error?: string } & T) | null;
    if (!response.ok || !body?.ok) {
      const code = body?.error ?? String(response.status);
      throw new SlackError(`Slack refused ${method}: ${code}`, code);
    }
    return body as T & { ok: true };
  }

  /**
   * Trade the callback's one-time `code` for the workspace's bot token. Refuses
   * what Ember can't ingest: an organization-wide Enterprise Grid install (no
   * one workspace), or an answer without a bot token.
   */
  async exchangeCode(code: string, redirectUri: string): Promise<Install> {
    const body = await this.call<{
      access_token?: string;
      token_type?: string;
      scope?: string;
      bot_user_id?: string;
      app_id?: string;
      team?: { id?: string; name?: string } | null;
      enterprise?: { id?: string } | null;
      is_enterprise_install?: boolean;
      authed_user?: { id?: string };
    }>("oauth.v2.access", { client_id: this.config.clientId, client_secret: this.config.clientSecret, code, redirect_uri: redirectUri });
    if (body.is_enterprise_install) {
      // Slack installed the app across the organization already. Take it back out, so it doesn't send
      // events Ember stores for no one; if that fails, an org admin removes it under Manage apps
      if (body.access_token) {
        await this.uninstall(body.access_token).catch((error: Error) => {
          console.warn(`[connect] Slack: couldn't uninstall a refused Enterprise Grid install: ${error.message}`);
        });
      }
      throw new SlackError("an organization-wide Enterprise Grid install isn't supported", "enterprise_install");
    }
    const teamId = body.team?.id;
    if (!isTeamId(teamId)) throw new SlackError("Slack named no workspace for the install", "no_team");
    if (body.token_type !== "bot" || !body.access_token?.startsWith("xoxb-") || !body.bot_user_id) {
      throw new SlackError("Slack sent no bot token", "no_bot_token");
    }
    return {
      teamId,
      teamName: body.team?.name || teamId,
      botToken: body.access_token,
      botUserId: body.bot_user_id,
      appId: body.app_id ?? "",
      scope: body.scope ?? "",
      installerId: body.authed_user?.id ?? "",
      enterpriseId: body.enterprise?.id ?? null,
    };
  }

  /** Uninstall the app from the workspace the token belongs to. Slack revokes the token and stops its events. */
  async uninstall(botToken: string): Promise<void> {
    await this.call("apps.uninstall", { client_id: this.config.clientId, client_secret: this.config.clientSecret, token: botToken });
  }
}

/** The bot scopes Slack granted, checked against BOT_SCOPES: the ones missing, if any. */
export function missingScopes(granted: string): string[] {
  const have = new Set(granted.split(",").map((s) => s.trim()));
  return BOT_SCOPES.filter((scope) => !have.has(scope));
}

// ---------------------------------------------------------------------------
// The `api` credential. Its JSON is what enrich:slack reads
// (services/slack-ingestion/workspaces.py, WorkspaceTokens), so the field names
// are snake_case and stable.

export interface SlackCredential {
  bot_token: string;
  /** Our bot's user id in the workspace: `tokens_revoked` naming it means the token is gone. */
  bot_user_id: string;
  scope: string;
  app_id: string;
  /** The Slack user who installed it. */
  installed_by: string;
  enterprise_id: string | null;
}

export function credentialFrom(install: Install): SlackCredential {
  return {
    bot_token: install.botToken,
    bot_user_id: install.botUserId,
    scope: install.scope,
    app_id: install.appId,
    installed_by: install.installerId,
    enterprise_id: install.enterpriseId,
  };
}

/** A stored credential, checked: one written by another version, or garbled, is refused rather than half-used. */
export function asSlackCredential(value: Record<string, unknown> | null): SlackCredential | null {
  if (!value || typeof value.bot_token !== "string" || typeof value.bot_user_id !== "string") return null;
  return {
    bot_token: value.bot_token,
    bot_user_id: value.bot_user_id,
    scope: typeof value.scope === "string" ? value.scope : "",
    app_id: typeof value.app_id === "string" ? value.app_id : "",
    installed_by: typeof value.installed_by === "string" ? value.installed_by : "",
    enterprise_id: typeof value.enterprise_id === "string" ? value.enterprise_id : null,
  };
}

// ---------------------------------------------------------------------------
// Connecting and disconnecting a workspace.

export interface ConnectWorkspaceInput {
  install: Install;
  tenant: string;
  userId: string;
}

/**
 * Map the workspace to the tenant and store its bot token, in one
 * transaction, then hand its stored deliveries to enrich (connectAccount).
 * A workspace another tenant holds is refused, and nothing is stored.
 * Reconnecting the same workspace to the same tenant clears
 * `disconnected_at` and replaces the token.
 */
export async function connectWorkspace(pool: pg.Pool, ring: KeyRing, { install, tenant, userId }: ConnectWorkspaceInput): Promise<ConnectResult> {
  return connectAccount(
    pool,
    { source: SOURCE, accountId: install.teamId, tenant, label: install.teamName, externalId: null, userId },
    { ring, secret: { ...credentialFrom(install) } },
  );
}

/**
 * Before Disconnect deletes the credential: uninstall the app from the
 * workspace with it (ADR 0010, Disconnect step 3). `deleted` when Slack
 * accepted, or the token was already dead; `failed` when Slack couldn't be
 * reached or refused for another reason; `none` when there was no credential.
 * An app left installed keeps sending events, and intake stores them without
 * a tenant.
 */
export async function uninstallWorkspace(pool: pg.Pool, ring: KeyRing, client: SlackClient, teamId: string): Promise<"deleted" | "failed" | "none"> {
  try {
    const credential = asSlackCredential(await loadCredential(pool, ring, { source: SOURCE, accountId: teamId }));
    if (!credential) return "none";
    await client.uninstall(credential.bot_token);
    return "deleted";
  } catch (error) {
    // The workspace already removed the app, or revoked the token: nothing is left to uninstall
    if (error instanceof SlackError && ["invalid_auth", "account_inactive", "token_revoked", "not_authed"].includes(error.code)) return "deleted";
    console.warn(`[connect] slack ${teamId}: couldn't uninstall the app on disconnect: ${(error as Error).message}`);
    return "failed";
  }
}
