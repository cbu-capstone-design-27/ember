// Connect Jira (EMBER-131, ADR 0010): an organization's owner consents to
// Ember's Atlassian OAuth 2.0 (3LO) app, picks one of the Jira sites the
// consent covers and the projects to ingest, and the web app:
//
// 1. registers a dynamic webhook on the site, filtered by JQL to those projects,
//    pointing at intake (JIRA_WEBHOOK_URL);
// 2. maps the site's host to the organization's tenant in `installations`;
// 3. stores the token pair as the site's `api` credential (`source_credentials`).
//
// Atlassian proves the site is theirs: accessible-resources lists only the
// sites the consenting user can use. The token and webhook belong to that
// user, so Settings asks owners to consent as a dedicated integration user.
//
// Server only: it holds the app's client secret and the api key ring.

import { randomBytes } from "node:crypto";
import type pg from "pg";
import { connectAccount } from "./connections.ts";
import { cookieSealer, sameState } from "./signed-cookies.ts";
import { KeyRing } from "./source-credentials.ts";

export const ATLASSIAN_AUTH = "https://auth.atlassian.com";
export const ATLASSIAN_API = "https://api.atlassian.com";
export const SOURCE = "jira";

/** Where Atlassian sends the browser back: the app's callback URL is this path on BETTER_AUTH_URL. */
export const CALLBACK_PATH = "/connect/jira/callback";
/** The cookies below are sent only to the connect routes and the picker. */
export const COOKIE_PATH = "/connect/jira";
/** The round trip through Atlassian: which organization, who started it, and the state Atlassian must echo. */
export const STATE_COOKIE = "ember_jira_connect";
/** Which pending consent the picker shows (PendingConsent). */
export const PICK_COOKIE = "ember_jira_pick";
export const STATE_TTL_SECONDS = 15 * 60;
export const PICK_TTL_SECONDS = 15 * 60;

/**
 * `offline_access` gets a refresh token. `manage:jira-webhook` registers, extends and deletes the
 * webhook; `read:jira-work` lists projects and reads issues; `read:jira-user` reads people on them.
 */
export const SCOPES = ["read:jira-work", "read:jira-user", "manage:jira-webhook", "offline_access"] as const;

/**
 * Every event a dynamic webhook can carry that the admin webhook also sent
 * (docs/ingestion/jira-webhooks.md). Worklog, attachment and issue link events
 * aren't offered to dynamic webhooks; they show as issue edits and reconcile reads them.
 */
export const WEBHOOK_EVENTS = [
  "jira:issue_created",
  "jira:issue_updated",
  "jira:issue_deleted",
  "comment_created",
  "comment_updated",
  "comment_deleted",
  "issue_property_set",
  "issue_property_deleted",
] as const;

/** Refresh an access token this long before it expires. */
export const REFRESH_MARGIN_SECONDS = 5 * 60;
/** Projects the picker lists, at most. */
export const MAX_PROJECTS = 500;

export interface JiraAppConfig {
  clientId: string;
  clientSecret: string;
  /** Intake's public address for Jira, e.g. https://ember.tail470a31.ts.net/webhook/jira. */
  webhookUrl: string;
}

/** The 3LO app's credentials and intake's address, or null while any is missing (Connect Jira is then unavailable). */
export function jiraAppConfig(env: Record<string, string | undefined> = process.env): JiraAppConfig | null {
  const clientId = env.JIRA_OAUTH_CLIENT_ID?.trim();
  const clientSecret = env.JIRA_OAUTH_CLIENT_SECRET?.trim();
  const webhookUrl = env.JIRA_WEBHOOK_URL?.trim();
  if (!clientId || !clientSecret || !webhookUrl) return null;
  try {
    if (new URL(webhookUrl).protocol !== "https:") return null;
  } catch {
    return null;
  }
  return { clientId, clientSecret, webhookUrl };
}

// The state and picker cookies are signed with a key only this flow uses.
export const { seal, unseal } = cookieSealer("ember:connect-jira");

/** The state cookie's content. `state` is what Atlassian has to send back. */
export interface ConnectState {
  state: string;
  organizationId: string;
  userId: string;
}

/** The picker cookie's content: which pending consent, for whom. */
export interface PickState {
  pendingId: string;
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

/** Atlassian's consent screen. `prompt=consent` shows it even to someone who consented before. */
export function authorizeUrl(config: JiraAppConfig, state: string, redirectUri: string): string {
  const query = new URLSearchParams({
    audience: "api.atlassian.com",
    client_id: config.clientId,
    scope: SCOPES.join(" "),
    redirect_uri: redirectUri,
    state,
    response_type: "code",
    prompt: "consent",
  });
  return `${ATLASSIAN_AUTH}/authorize?${query}`;
}

// ---------------------------------------------------------------------------
// Atlassian's API.

export interface TokenSet {
  accessToken: string;
  refreshToken: string;
  /** Epoch seconds. */
  expiresAt: number;
  scope: string;
}

export interface Site {
  cloudId: string;
  /** What intake derives from a delivery's `self` URLs, and so `account_id`: e.g. acme.atlassian.net. */
  host: string;
  name: string;
}

export interface Project {
  id: string;
  key: string;
  name: string;
}

export class AtlassianError extends Error {
  readonly status: number | undefined;
  /** Atlassian's `error` code on a token request, e.g. invalid_grant. */
  readonly code: string | undefined;

  constructor(message: string, status?: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

type Fetch = typeof fetch;

/** The host of a site's URL, e.g. acme.atlassian.net, or null when it isn't an https URL. */
export function siteHost(url: unknown): string | null {
  if (typeof url !== "string") return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname ? parsed.hostname.toLowerCase() : null;
  } catch {
    return null;
  }
}

/** JQL for the projects an owner picked, by id: a key changes when a project is renamed. */
export function projectJql(projectIds: string[]): string {
  if (projectIds.length === 0 || projectIds.some((id) => !/^[0-9]{1,20}$/.test(id))) {
    throw new Error("project ids are numeric, and at least one is needed");
  }
  return `project in (${projectIds.join(", ")})`;
}

export class AtlassianClient {
  private readonly config: JiraAppConfig;
  private readonly fetchImpl: Fetch;

  constructor(config: JiraAppConfig, fetchImpl: Fetch = fetch) {
    this.config = config;
    this.fetchImpl = fetchImpl;
  }

  private async token(grant: Record<string, string>, now: number): Promise<TokenSet> {
    const response = await this.fetchImpl(`${ATLASSIAN_AUTH}/oauth/token`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify({ client_id: this.config.clientId, client_secret: this.config.clientSecret, ...grant }),
      signal: AbortSignal.timeout(10_000),
    });
    const body = (await response.json().catch(() => null)) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
      scope?: string;
      error?: string;
    } | null;
    if (!response.ok || !body?.access_token) {
      throw new AtlassianError(`Atlassian refused the ${grant.grant_type} grant: ${body?.error ?? response.status}`, response.status, body?.error);
    }
    if (!body.refresh_token) throw new AtlassianError("Atlassian sent no refresh token; offline_access wasn't granted");
    return {
      accessToken: body.access_token,
      refreshToken: body.refresh_token,
      expiresAt: Math.floor(now / 1000) + (typeof body.expires_in === "number" ? body.expires_in : 3600),
      scope: body.scope ?? "",
    };
  }

  /** Trade the callback's one-time `code` for a token pair. */
  exchangeCode(code: string, redirectUri: string, now = Date.now()): Promise<TokenSet> {
    return this.token({ grant_type: "authorization_code", code, redirect_uri: redirectUri }, now);
  }

  /** A new token pair. Atlassian rotates refresh tokens, so store the one this returns. */
  refresh(refreshToken: string, now = Date.now()): Promise<TokenSet> {
    return this.token({ grant_type: "refresh_token", refresh_token: refreshToken }, now);
  }

  private async api<T>(token: string, method: string, path: string, body?: unknown): Promise<T | null> {
    const response = await this.fetchImpl(`${ATLASSIAN_API}${path}`, {
      method,
      headers: {
        accept: "application/json",
        authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      const detail = (await response.json().catch(() => null)) as { errorMessages?: string[] } | null;
      const message = detail?.errorMessages?.[0];
      throw new AtlassianError(`Atlassian answered ${response.status} to ${method} ${path.split("?")[0]}${message ? `: ${message}` : ""}`, response.status);
    }
    const text = await response.text();
    return text ? (JSON.parse(text) as T) : null;
  }

  /** The Jira sites the token can use. */
  async sites(token: string): Promise<Site[]> {
    const resources = (await this.api<{ id: string; url: string; name: string; scopes?: string[] }[]>(token, "GET", "/oauth/token/accessible-resources")) ?? [];
    return resources.flatMap((r) => {
      const host = siteHost(r.url);
      // A resource without Jira scopes is another product's (Confluence) on the same consent
      if (!host || typeof r.id !== "string" || !(r.scopes ?? []).includes("read:jira-work")) return [];
      return [{ cloudId: r.id, host, name: r.name || host }];
    });
  }

  /** The site's projects the token can see, by name, up to MAX_PROJECTS. `more` says some were left out. */
  async projects(token: string, cloudId: string): Promise<{ projects: Project[]; more: boolean }> {
    const projects: Project[] = [];
    for (let startAt = 0; projects.length < MAX_PROJECTS; ) {
      const page = await this.api<{ values: { id: string; key: string; name: string }[]; isLast?: boolean }>(
        token,
        "GET",
        `/ex/jira/${encodeURIComponent(cloudId)}/rest/api/3/project/search?orderBy=name&maxResults=50&startAt=${startAt}`,
      );
      const values = page?.values ?? [];
      projects.push(...values.map(({ id, key, name }) => ({ id: String(id), key, name })));
      if (page?.isLast !== false || values.length === 0) return { projects, more: false };
      startAt += values.length;
    }
    return { projects: projects.slice(0, MAX_PROJECTS), more: true };
  }

  /** The Atlassian account that consented. */
  async myself(token: string, cloudId: string): Promise<{ accountId: string; displayName: string }> {
    const me = await this.api<{ accountId: string; displayName: string }>(token, "GET", `/ex/jira/${encodeURIComponent(cloudId)}/rest/api/3/myself`);
    return { accountId: me?.accountId ?? "", displayName: me?.displayName ?? "" };
  }

  /** Register one webhook for WEBHOOK_EVENTS on `jql`, sent to `url`. Returns its id. */
  async registerWebhook(token: string, cloudId: string, jql: string): Promise<number> {
    const result = await this.api<{ webhookRegistrationResult?: { createdWebhookId?: number; errors?: string[] }[] }>(
      token,
      "POST",
      `/ex/jira/${encodeURIComponent(cloudId)}/rest/api/3/webhook`,
      { url: this.config.webhookUrl, webhooks: [{ events: WEBHOOK_EVENTS, jqlFilter: jql }] },
    );
    const registered = result?.webhookRegistrationResult?.[0];
    if (typeof registered?.createdWebhookId !== "number") {
      throw new AtlassianError(`Jira didn't register the webhook: ${registered?.errors?.join("; ") || "no reason given"}`);
    }
    return registered.createdWebhookId;
  }

  /** Delete webhooks this app registered. Ids that aren't the app's, or are gone, are ignored by Jira. */
  async deleteWebhooks(token: string, cloudId: string, webhookIds: number[]): Promise<void> {
    if (webhookIds.length === 0) return;
    await this.api(token, "DELETE", `/ex/jira/${encodeURIComponent(cloudId)}/rest/api/3/webhook`, { webhookIds });
  }
}

// ---------------------------------------------------------------------------
// The `api` credential. Its JSON is what enrich:jira and the keep-alive read
// (services/jira-ingestion), so the field names are snake_case and stable.

export interface JiraCredential {
  access_token: string;
  refresh_token: string;
  /** Epoch seconds. */
  expires_at: number;
  scope: string;
  cloud_id: string;
  /** The dynamic webhooks Ember registered on the site, to extend and to delete. */
  webhook_ids: number[];
  jql: string;
  /** Who consented: the token and the webhooks are theirs. */
  atlassian_account_id: string;
  atlassian_name: string;
}

export function credentialFrom(tokens: TokenSet, rest: Omit<JiraCredential, "access_token" | "refresh_token" | "expires_at" | "scope">): JiraCredential {
  return { access_token: tokens.accessToken, refresh_token: tokens.refreshToken, expires_at: tokens.expiresAt, scope: tokens.scope, ...rest };
}

/** A stored credential, checked: one written by another version, or garbled, is refused rather than half-used. */
export function asJiraCredential(value: Record<string, unknown> | null): JiraCredential | null {
  if (!value) return null;
  const { access_token, refresh_token, expires_at, cloud_id, webhook_ids } = value;
  if (typeof access_token !== "string" || typeof refresh_token !== "string" || typeof expires_at !== "number" || typeof cloud_id !== "string") return null;
  if (!Array.isArray(webhook_ids) || !webhook_ids.every((id) => typeof id === "number")) return null;
  return {
    access_token,
    refresh_token,
    expires_at,
    cloud_id,
    webhook_ids,
    scope: typeof value.scope === "string" ? value.scope : "",
    jql: typeof value.jql === "string" ? value.jql : "",
    atlassian_account_id: typeof value.atlassian_account_id === "string" ? value.atlassian_account_id : "",
    atlassian_name: typeof value.atlassian_name === "string" ? value.atlassian_name : "",
  };
}

/**
 * The site's stored credential with a live access token, refreshing it first
 * when it is about to expire, or null when the site has no credential.
 *
 * The row is locked while refreshing, because Atlassian rotates refresh
 * tokens: two processes refreshing the same one at once would leave one of
 * them holding a token that no longer works. enrich:jira and the keep-alive
 * lock the same row. The new pair is committed before it is used, so a call
 * that fails afterwards can't lose it.
 */
export async function freshJiraCredential(pool: pg.Pool, ring: KeyRing, client: AtlassianClient, host: string, now = Date.now): Promise<JiraCredential | null> {
  const row = { source: SOURCE, accountId: host };
  const db = await pool.connect();
  try {
    await db.query("begin");
    const { rows } = await db.query<{ key_id: string; ciphertext: Buffer }>(
      "select key_id, ciphertext from public.source_credentials where source = $1 and account_id = $2 and kind = 'api' for update",
      [SOURCE, host],
    );
    if (rows.length === 0) {
      await db.query("rollback");
      return null;
    }
    let credential = asJiraCredential(ring.open(row, rows[0].key_id, rows[0].ciphertext));
    if (!credential) throw new AtlassianError(`the jira credential for ${host} isn't one Connect Jira wrote`);
    if (credential.expires_at - REFRESH_MARGIN_SECONDS <= now() / 1000) {
      const tokens = await client.refresh(credential.refresh_token, now());
      credential = { ...credential, access_token: tokens.accessToken, refresh_token: tokens.refreshToken, expires_at: tokens.expiresAt, scope: tokens.scope || credential.scope };
      const sealed = ring.seal(row, { ...credential });
      await db.query(
        "update public.source_credentials set key_id = $3, ciphertext = $4, updated_at = now() where source = $1 and account_id = $2 and kind = 'api'",
        [SOURCE, host, sealed.keyId, sealed.ciphertext],
      );
    }
    await db.query("commit");
    return credential;
  } catch (error) {
    await db.query("rollback").catch(() => {});
    throw error;
  } finally {
    db.release();
  }
}

// ---------------------------------------------------------------------------
// The consent between the callback and the picker. The tokens never reach the
// browser: they are sealed with the api key ring and kept server side, in
// Better Auth's verification table, for PICK_TTL_SECONDS. The picker cookie
// names the row.

export interface PendingSite extends Site {
  projects: Project[];
  /** The site had more projects than the picker lists. */
  more: boolean;
  /** Why it can't be connected, or null when it can. */
  blocked: string | null;
}

export interface PendingConsent {
  organizationId: string;
  userId: string;
  tokens: TokenSet;
  atlassian: { accountId: string; displayName: string };
  sites: PendingSite[];
}

/** Better Auth's verification store, as its internal adapter exposes it. */
export interface VerificationStore {
  createVerificationValue(data: { identifier: string; value: string; expiresAt: Date }): Promise<unknown>;
  findVerificationValue(identifier: string): Promise<{ value: string; expiresAt: Date } | null>;
  deleteVerificationByIdentifier(identifier: string): Promise<void>;
}

const pendingIdentifier = (id: string) => `connect-jira:${id}`;
// The AAD names the pending row, so a sealed consent can't be replayed as a stored credential
const pendingRow = (id: string) => ({ source: SOURCE, accountId: `pending:${id}` });

export async function savePending(store: VerificationStore, ring: KeyRing, consent: PendingConsent, now = Date.now()): Promise<string> {
  const id = randomBytes(18).toString("base64url");
  const { keyId, ciphertext } = ring.seal(pendingRow(id), { ...consent });
  await store.createVerificationValue({
    identifier: pendingIdentifier(id),
    value: JSON.stringify({ keyId, ciphertext: ciphertext.toString("base64") }),
    expiresAt: new Date(now + PICK_TTL_SECONDS * 1000),
  });
  return id;
}

/** The pending consent `id` names, if it exists, hasn't expired, and belongs to this organization and user. */
export async function loadPending(store: VerificationStore, ring: KeyRing, pick: PickState, now = Date.now()): Promise<PendingConsent | null> {
  if (!/^[A-Za-z0-9_-]{24}$/.test(pick.pendingId)) return null;
  const found = await store.findVerificationValue(pendingIdentifier(pick.pendingId));
  if (!found || found.expiresAt.getTime() <= now) return null;
  try {
    const { keyId, ciphertext } = JSON.parse(found.value) as { keyId: string; ciphertext: string };
    const consent = ring.open(pendingRow(pick.pendingId), keyId, Buffer.from(ciphertext, "base64")) as unknown as PendingConsent;
    return consent.organizationId === pick.organizationId && consent.userId === pick.userId ? consent : null;
  } catch {
    return null;
  }
}

export async function deletePending(store: VerificationStore, pendingId: string): Promise<void> {
  await store.deleteVerificationByIdentifier(pendingIdentifier(pendingId));
}

// ---------------------------------------------------------------------------
// Connecting and disconnecting a site.

export interface ConnectSiteInput {
  consent: PendingConsent;
  site: PendingSite;
  /** The projects the owner ticked; only the site's own are kept. */
  projectIds: string[];
  tenant: string;
  userId: string;
}

export type ConnectSiteResult =
  | { ok: true; host: string; reconnected: boolean }
  /** The site belongs to another tenant (ADR 0010: never moved). */
  | { ok: false; reason: "other-tenant" }
  /** None of the ticked projects is on the site. */
  | { ok: false; reason: "no-projects" };

/**
 * Register the webhook, map the site, store the credential, then remove the
 * webhooks an earlier connection of the same site registered. The webhook
 * comes first: a delivery that lands before the mapping commits is stored
 * without a tenant, and the hand-off gives it this one. If the mapping is
 * refused, the new webhook is deleted again.
 */
export async function connectSite(
  pool: pg.Pool,
  ring: KeyRing,
  client: AtlassianClient,
  input: ConnectSiteInput,
): Promise<ConnectSiteResult> {
  const { consent, site } = input;
  const offered = new Set(site.projects.map((p) => p.id));
  const projectIds = [...new Set(input.projectIds)].filter((id) => offered.has(id));
  if (projectIds.length === 0) return { ok: false, reason: "no-projects" };
  const jql = projectJql(projectIds);
  const token = consent.tokens.accessToken;

  // The earlier connection's webhooks, read before the new credential replaces them
  let previous: JiraCredential | null = null;
  try {
    const { rows } = await pool.query<{ key_id: string; ciphertext: Buffer; tenant: string }>(
      `select c.key_id, c.ciphertext, i.tenant from public.source_credentials c
       join public.installations i using (source, account_id)
       where c.source = $1 and c.account_id = $2 and c.kind = 'api'`,
      [SOURCE, site.host],
    );
    if (rows[0]?.tenant === input.tenant) previous = asJiraCredential(ring.open({ source: SOURCE, accountId: site.host }, rows[0].key_id, rows[0].ciphertext));
  } catch {
    // A credential that won't open is replaced below; its webhooks lapse within 30 days
  }

  const webhookId = await client.registerWebhook(token, site.cloudId, jql);
  const connection = { source: SOURCE, accountId: site.host, tenant: input.tenant, label: site.host, externalId: site.cloudId, userId: input.userId };
  let result: Awaited<ReturnType<typeof connectAccount>>;
  try {
    result = await connectAccount(pool, connection);
  } catch (error) {
    await client.deleteWebhooks(token, site.cloudId, [webhookId]).catch(() => {});
    throw error;
  }
  if (!result.ok) {
    await client.deleteWebhooks(token, site.cloudId, [webhookId]).catch(() => {});
    return result;
  }
  const credential = credentialFrom(consent.tokens, {
    cloud_id: site.cloudId,
    webhook_ids: [webhookId],
    jql,
    atlassian_account_id: consent.atlassian.accountId,
    atlassian_name: consent.atlassian.displayName,
  });
  const { keyId, ciphertext } = ring.seal({ source: SOURCE, accountId: site.host }, { ...credential });
  await pool.query(
    `insert into public.source_credentials (source, account_id, kind, key_id, ciphertext)
     values ($1, $2, 'api', $3, $4)
     on conflict (source, account_id, kind) do update
       set key_id = excluded.key_id, ciphertext = excluded.ciphertext, updated_at = now()`,
    [SOURCE, site.host, keyId, ciphertext],
  );
  // Wake enrich:jira to backfill the site (EMBER-167). connectAccount's hand-off notified before this credential existed
  await pool.query("select pg_notify('raw_events', $1)", [SOURCE]);
  const stale = (previous?.webhook_ids ?? []).filter((id) => id !== webhookId);
  if (stale.length > 0) {
    // Best effort: another user's webhook may not be ours to delete, and it lapses within 30 days
    await client.deleteWebhooks(token, site.cloudId, stale).catch((error: Error) => {
      console.warn(`[connect] jira ${site.host}: couldn't delete the earlier webhooks: ${error.message}`);
    });
  }
  return { ok: true, host: site.host, reconnected: result.reconnected };
}

/**
 * Before Disconnect deletes the credential: delete the site's dynamic
 * webhooks with it. `deleted` when Jira accepted, `failed` when it couldn't
 * be done (the consenting user lost access, Atlassian was down), `none` when
 * there was no credential. A webhook left behind lapses within 30 days, and
 * meanwhile intake stores its deliveries without a tenant.
 */
export async function removeWebhooks(pool: pg.Pool, ring: KeyRing, client: AtlassianClient, host: string): Promise<"deleted" | "failed" | "none"> {
  try {
    const credential = await freshJiraCredential(pool, ring, client, host);
    if (!credential) return "none";
    await client.deleteWebhooks(credential.access_token, credential.cloud_id, credential.webhook_ids);
    return "deleted";
  } catch (error) {
    console.warn(`[connect] jira ${host}: couldn't delete the webhooks on disconnect: ${(error as Error).message}`);
    return "failed";
  }
}
