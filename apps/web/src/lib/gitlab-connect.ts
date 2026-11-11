// Connect GitLab (EMBER-149, ADR 0010, GitLab): GitLab has no install flow
// that works across GitLab.com and self-managed instances without an OAuth app
// on each, so an owner pastes the connection:
//
// 1. the instance URL, the top-level group, and a `read_api` token: a Group
//    Access Token on Premium or Ultimate, a Personal Access Token on Free;
// 2. the web app reads the group with that token. The token's user must be a
//    member of it, since anyone can read a public group. A Group Access Token
//    (its user is the group's bot) can be made only by the group's Owners, so
//    it is strong proof; a Personal Access Token proves only that someone in
//    the group pasted it. That is accepted, and its label says so;
// 3. it maps `<instance host>/<group>`, the `account_id` intake derives from a
//    delivery, to the tenant, and stores the token as the group's `api` credential;
// 4. the shared hand-off runs (lib/connections.ts);
// 5. the owner adds a webhook pointing at intake to the group (Premium) or to
//    each project (Free), with GitLab's **Generate signing token**, and pastes
//    each webhook's token back (addSigningToken). They are the group's
//    `webhook` credential, which intake verifies its deliveries with. GitLab's
//    page can't take a token Ember made (only its API can, with the `api`
//    write scope), and each webhook gets its own: ADR 0010, amended for EMBER-149.
//
// Server only: it holds the tokens and both key rings.

import type pg from "pg";
import { connectAccount } from "./connections.ts";
import { KeyRing, loadCredential, storeCredential } from "./source-credentials.ts";

export const SOURCE = "gitlab";
export const DEFAULT_INSTANCE = "https://gitlab.com";
const API_PREFIX = "/api/v4";

/**
 * The webhook triggers to tick, as `RECOMMENDED_WEBHOOK_TRIGGERS` in
 * services/gitlab-ingestion/receiver.py lists them (docs/ingestion/gitlab-webhooks.md).
 */
export const WEBHOOK_TRIGGERS = ["Push events", "Comments", "Issues events", "Merge request events"] as const;
/** Signing tokens kept per group: one per webhook, so one per project on Free. */
export const MAX_SIGNING_TOKENS = 200;

/** Scopes that write. Every Ember source credential is read-only (ADR 0010), so a token with one is refused. */
const WRITE_SCOPES = new Set(["api", "sudo", "admin_mode", "write_repository", "write_registry", "write_virtual_registry", "manage_runner", "create_runner"]);

export interface GitLabConnectConfig {
  /** Intake's public address for GitLab, e.g. https://ember.tail470a31.ts.net/webhook/gitlab. */
  webhookUrl: string;
}

/** Intake's address for GitLab, or null while it isn't set (Connect GitLab is then unavailable). */
export function gitlabConnectConfig(env: Record<string, string | undefined> = process.env): GitLabConnectConfig | null {
  const webhookUrl = env.GITLAB_WEBHOOK_URL?.trim();
  if (!webhookUrl) return null;
  try {
    if (new URL(webhookUrl).protocol !== "https:") return null;
  } catch {
    return null;
  }
  return { webhookUrl };
}

export interface Instance {
  /** Where the API lives, without /api/v4, e.g. https://gitlab.example.com/gitlab. */
  baseUrl: string;
  /** The host as intake derives it from X-Gitlab-Instance: lower case, no port. */
  host: string;
}

/**
 * The instance an owner typed, or null when it isn't a plain https URL. The
 * token goes there, so http, credentials in the URL, a query and a fragment are refused.
 */
export function parseInstance(raw: string): Instance | null {
  const text = raw.trim().replace(/\/+$/, "") || DEFAULT_INSTANCE;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.search || url.hash) return null;
  const path = url.pathname.replace(/\/+$/, "");
  if (path.endsWith(API_PREFIX)) return null;
  return { baseUrl: `${url.origin}${path}`, host: url.hostname.toLowerCase() };
}

/** A top-level group's path, e.g. `acme`, or null. A subgroup (`acme/platform`) isn't an account. */
export function parseGroup(raw: string): string | null {
  const path = raw.trim().replace(/^\/+|\/+$/g, "");
  return /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$/.test(path) && !path.endsWith(".git") && !path.endsWith(".atom") ? path : null;
}

export class GitLabError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

type Fetch = typeof fetch;

export interface TokenInfo {
  userId: number;
  scopes: string[];
  active: boolean;
  /** YYYY-MM-DD, or null for a token that never expires. */
  expiresAt: string | null;
}

export interface GitLabUser {
  id: number;
  username: string;
  /** True for the user behind a group or project access token. */
  bot: boolean;
}

export interface GitLabGroup {
  id: number;
  fullPath: string;
  name: string;
  parentId: number | null;
}

/** The few GitLab API calls Connect GitLab makes, with the pasted token. */
export class GitLabClient {
  private readonly api: string;
  private readonly token: string;
  private readonly fetchImpl: Fetch;

  constructor(instance: Instance, token: string, fetchImpl: Fetch = fetch) {
    this.api = `${instance.baseUrl}${API_PREFIX}`;
    this.token = token;
    this.fetchImpl = fetchImpl;
  }

  /** GET `path`; null on a 404. Redirects aren't followed, so the token never goes to another address. */
  private async get<T>(path: string): Promise<T | null> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.api}${path}`, {
        headers: { accept: "application/json", "private-token": this.token },
        redirect: "manual",
        signal: AbortSignal.timeout(10_000),
      });
    } catch (error) {
      throw new GitLabError(`GitLab didn't answer GET ${path.split("?")[0]}: ${(error as Error).name}`);
    }
    if (response.status === 404) return null;
    if (!response.ok) throw new GitLabError(`GitLab answered ${response.status} to GET ${path.split("?")[0]}`, response.status);
    const body = (await response.json().catch(() => null)) as T | null;
    if (body === null) throw new GitLabError(`GitLab sent no JSON for GET ${path.split("?")[0]}`, response.status);
    return body;
  }

  /** The pasted token itself (GitLab 15.5+). Group and project access tokens are personal access tokens of their bot. */
  async tokenSelf(): Promise<TokenInfo> {
    const body = await this.get<{ user_id?: number; scopes?: string[]; active?: boolean; expires_at?: string | null }>("/personal_access_tokens/self");
    if (!body || typeof body.user_id !== "number") throw new GitLabError("GitLab didn't describe the token");
    return { userId: body.user_id, scopes: body.scopes ?? [], active: body.active !== false, expiresAt: body.expires_at ?? null };
  }

  async user(): Promise<GitLabUser> {
    const body = await this.get<{ id?: number; username?: string; bot?: boolean }>("/user");
    if (!body || typeof body.id !== "number") throw new GitLabError("GitLab didn't say whose token it is");
    return { id: body.id, username: body.username ?? "", bot: body.bot === true };
  }

  /** The group, or null when the token can't see it. */
  async group(path: string): Promise<GitLabGroup | null> {
    const body = await this.get<{ id?: number; full_path?: string; name?: string; parent_id?: number | null }>(
      `/groups/${encodeURIComponent(path)}?with_projects=false`,
    );
    if (!body) return null;
    if (typeof body.id !== "number" || typeof body.full_path !== "string") throw new GitLabError("GitLab sent a group without an id or path");
    return { id: body.id, fullPath: body.full_path, name: body.name || body.full_path, parentId: body.parent_id ?? null };
  }

  /** The user's access level in the group, inherited or direct, or null when they aren't a member. */
  async accessLevel(groupId: number, userId: number): Promise<number | null> {
    const body = await this.get<{ access_level?: number }>(`/groups/${groupId}/members/all/${userId}`);
    return typeof body?.access_level === "number" ? body.access_level : null;
  }
}

export type TokenKind = "group" | "personal";

export interface VerifiedGroup {
  group: GitLabGroup;
  user: GitLabUser;
  tokenKind: TokenKind;
  scopes: string[];
  expiresAt: string | null;
}

export type VerifyFailure =
  /** GitLab refused the token (401), or it was revoked or expired. */
  | "invalid-token"
  /** The token lacks read_api. */
  | "scope"
  /** The token can write; Ember stores only read-only credentials. */
  | "write-scope"
  /** The token can't see the group, or there is no such group. */
  | "not-found"
  /** It's a subgroup: the account is the top-level group. */
  | "not-top-level"
  /** The group is visible (public) but the token's user isn't a member, so it proves nothing. */
  | "not-member";

/** Read the group with the token: being a member of it is the proof (ADR 0010). */
export async function verifyGroup(client: GitLabClient, groupPath: string): Promise<{ ok: true; value: VerifiedGroup } | { ok: false; reason: VerifyFailure }> {
  let token: TokenInfo;
  try {
    token = await client.tokenSelf();
  } catch (error) {
    if (error instanceof GitLabError && error.status === 401) return { ok: false, reason: "invalid-token" };
    throw error;
  }
  if (!token.active) return { ok: false, reason: "invalid-token" };
  if (token.scopes.some((scope) => WRITE_SCOPES.has(scope))) return { ok: false, reason: "write-scope" };
  if (!token.scopes.includes("read_api")) return { ok: false, reason: "scope" };
  const user = await client.user();
  const group = await client.group(groupPath);
  if (!group) return { ok: false, reason: "not-found" };
  if (group.parentId !== null) return { ok: false, reason: "not-top-level" };
  if ((await client.accessLevel(group.id, token.userId)) === null) return { ok: false, reason: "not-member" };
  // The bot of a group access token is a member of its own group only, so a bot member of the top-level group is that group's token
  const tokenKind: TokenKind = user.bot ? "group" : "personal";
  return { ok: true, value: { group, user, tokenKind, scopes: token.scopes, expiresAt: token.expiresAt } };
}

/** The connection's name in Settings. A personal access token is labelled as the weaker proof it is. */
export function connectionLabel(group: GitLabGroup, kind: TokenKind, username: string): string {
  return kind === "group" ? group.name : `${group.name} (personal access token of @${username})`;
}

/**
 * A signing token as GitLab shows it once a webhook is saved: `whsec_` and
 * the base64 of a 32-byte key (GitLab's webhook API says so). Null when the
 * paste isn't one, e.g. cut short. Intake reads it the same way.
 */
export function parseSigningToken(raw: string): string | null {
  const token = raw.trim();
  const match = /^whsec_([A-Za-z0-9+/]+={0,2})$/.exec(token);
  if (!match || match[1].length % 4 !== 0) return null;
  return Buffer.from(match[1], "base64").length === 32 ? token : null;
}

/**
 * The `api` credential. Its JSON is what enrich:gitlab reads
 * (services/gitlab-ingestion/enrich.py), so the field names are snake_case and stable.
 */
export interface GitLabCredential {
  token: string;
  /** enrich calls only here; its host is the first half of the account id. */
  base_url: string;
  token_kind: TokenKind;
  username: string;
  user_id: number;
  group_id: number;
  group_path: string;
  scopes: string[];
  expires_at: string | null;
}

export interface ConnectGroupInput {
  instance: string;
  group: string;
  token: string;
  tenant: string;
  userId: string;
}

export type ConnectGroupResult =
  | {
      ok: true;
      accountId: string;
      label: string;
      tokenKind: TokenKind;
      webhookUrl: string;
      expiresAt: string | null;
      reconnected: boolean;
    }
  | { ok: false; reason: VerifyFailure | "invalid-instance" | "invalid-group" | "no-token" | "unreachable" | "other-tenant" };

/**
 * Check the pasted connection, map the group, and store its token. A
 * reconnect replaces the token and keeps the webhooks' signing tokens.
 */
export async function connectGroup(
  pool: pg.Pool,
  apiRing: KeyRing,
  config: GitLabConnectConfig,
  input: ConnectGroupInput,
  fetchImpl: Fetch = fetch,
): Promise<ConnectGroupResult> {
  const instance = parseInstance(input.instance);
  if (!instance) return { ok: false, reason: "invalid-instance" };
  const groupPath = parseGroup(input.group);
  if (!groupPath) return { ok: false, reason: "invalid-group" };
  const token = input.token.trim();
  if (!token) return { ok: false, reason: "no-token" };

  let verified: Awaited<ReturnType<typeof verifyGroup>>;
  try {
    verified = await verifyGroup(new GitLabClient(instance, token, fetchImpl), groupPath);
  } catch (error) {
    if (error instanceof GitLabError) {
      console.warn(`[connect] gitlab ${instance.host}/${groupPath}: ${error.message}`);
      return { ok: false, reason: "unreachable" };
    }
    throw error;
  }
  if (!verified.ok) return verified;
  const { group, user, tokenKind, scopes, expiresAt } = verified.value;

  // What intake derives: X-Gitlab-Instance's host and the first segment of path_with_namespace, as GitLab writes it
  const accountId = `${instance.host}/${group.fullPath}`;
  const label = connectionLabel(group, tokenKind, user.username);
  const connected = await connectAccount(pool, {
    source: SOURCE,
    accountId,
    tenant: input.tenant,
    label,
    // The group's id, per instance: a renamed group is the same account, so it can't move tenants either
    externalId: `${instance.host}:${group.id}`,
    userId: input.userId,
  });
  if (!connected.ok) return connected;

  const credential: GitLabCredential = {
    token,
    base_url: instance.baseUrl,
    token_kind: tokenKind,
    username: user.username,
    user_id: user.id,
    group_id: group.id,
    group_path: group.fullPath,
    scopes,
    expires_at: expiresAt,
  };
  await storeCredential(pool, apiRing, { source: SOURCE, accountId }, { ...credential });
  return {
    ok: true,
    accountId,
    label,
    tokenKind,
    webhookUrl: config.webhookUrl,
    expiresAt,
    reconnected: connected.reconnected,
  };
}

export type AddSigningTokenResult =
  /** `count`: how many webhooks' tokens the group has now. `already`: this one was stored before. */
  | { ok: true; count: number; already: boolean }
  | { ok: false; reason: "invalid-signing-token" | "not-found" | "limit" };

/**
 * Add one webhook's signing token to the group's `webhook` credential,
 * `{"signing_tokens": [...]}`, which intake opens. Only a connected group of
 * this tenant takes one. From the first one on, the group's deliveries verify
 * only with its own tokens.
 */
export async function addSigningToken(
  pool: pg.Pool,
  webhookRing: KeyRing,
  input: { accountId: string; tenant: string; token: string },
): Promise<AddSigningTokenResult> {
  const token = parseSigningToken(input.token);
  if (!token) return { ok: false, reason: "invalid-signing-token" };
  const row = { source: SOURCE, accountId: input.accountId };
  const client = await pool.connect();
  try {
    await client.query("begin");
    // The connection's row, locked, so two pastes at once both land and a Disconnect waits
    const { rowCount } = await client.query(
      `select 1 from public.installations
       where source = $1 and account_id = $2 and tenant = $3 and disconnected_at is null for update`,
      [SOURCE, input.accountId, input.tenant],
    );
    if (!rowCount) {
      await client.query("rollback");
      return { ok: false, reason: "not-found" };
    }
    const stored = await loadCredential(client, webhookRing, row);
    const tokens = Array.isArray(stored?.signing_tokens) ? stored.signing_tokens.filter((t): t is string => typeof t === "string") : [];
    if (tokens.includes(token)) {
      await client.query("rollback");
      return { ok: true, count: tokens.length, already: true };
    }
    if (tokens.length >= MAX_SIGNING_TOKENS) {
      await client.query("rollback");
      return { ok: false, reason: "limit" };
    }
    tokens.push(token);
    await storeCredential(client, webhookRing, row, { signing_tokens: tokens });
    await client.query("commit");
    return { ok: true, count: tokens.length, already: false };
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}

/** How many webhooks' signing tokens a connected group of this tenant has, or null when it isn't one. */
export async function signingTokenCount(pool: pg.Pool, webhookRing: KeyRing, accountId: string, tenant: string): Promise<number | null> {
  const { rowCount } = await pool.query(
    "select 1 from public.installations where source = $1 and account_id = $2 and tenant = $3 and disconnected_at is null",
    [SOURCE, accountId, tenant],
  );
  if (!rowCount) return null;
  const stored = await loadCredential(pool, webhookRing, { source: SOURCE, accountId });
  return Array.isArray(stored?.signing_tokens) ? stored.signing_tokens.length : 0;
}
