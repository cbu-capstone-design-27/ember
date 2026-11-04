// Connect GitHub (EMBER-130, ADR 0010): an organization's owner installs the
// `ember-ingest` App, or picks an installation that already exists, and the
// web app maps it to the organization's tenant. GitHub proves the
// installation is theirs: the callback trades the `code` it sends for a user
// access token, and accepts an installation only if `GET /user/installations`
// lists it for that user. The `installation_id` on the redirect alone can be
// spoofed. The token is revoked as soon as the check is done.
//
// Server only: it holds the App's client secret.

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const GITHUB_API = "https://api.github.com";
export const GITHUB_WEB = "https://github.com";

/** Where GitHub sends the browser back: the App's Callback URL is this path on BETTER_AUTH_URL. */
export const CALLBACK_PATH = "/connect/github/callback";

/** The cookies below are sent only to the connect routes. */
export const COOKIE_PATH = "/connect/github";
/** The round trip through GitHub: which organization, who started it, and the state GitHub must echo. */
export const STATE_COOKIE = "ember_github_connect";
/** The installations the existing-installation path offers, checked once at the callback. */
export const PICK_COOKIE = "ember_github_pick";
export const STATE_TTL_SECONDS = 15 * 60;
export const PICK_TTL_SECONDS = 10 * 60;

export interface GitHubAppConfig {
  /** The App's URL name: github.com/apps/<slug>. */
  slug: string;
  clientId: string;
  clientSecret: string;
}

/** The App's OAuth credentials, or null while the Secret isn't there (Connect GitHub is then unavailable). */
export function githubAppConfig(env: Record<string, string | undefined> = process.env): GitHubAppConfig | null {
  const clientId = env.GITHUB_APP_CLIENT_ID?.trim();
  const clientSecret = env.GITHUB_APP_CLIENT_SECRET?.trim();
  if (!clientId || !clientSecret) return null;
  return { slug: env.GITHUB_APP_SLUG?.trim() || "ember-ingest", clientId, clientSecret };
}

// ---------------------------------------------------------------------------
// Signed cookies. The browser holds them, so they are signed: otherwise a
// person could point their own round trip at another organization or add an
// installation to the picker. The key is derived from BETTER_AUTH_SECRET.

function signingKey(secret: string) {
  return createHmac("sha256", secret).update("ember:connect-github").digest();
}

function mac(payload: string, secret: string) {
  return createHmac("sha256", signingKey(secret)).update(payload).digest("base64url");
}

/** `value` as `<payload>.<signature>`, valid for `ttlSeconds`. */
export function seal(value: object, secret: string, ttlSeconds: number, now = Date.now()): string {
  const payload = Buffer.from(JSON.stringify({ ...value, exp: Math.floor(now / 1000) + ttlSeconds })).toString("base64url");
  return `${payload}.${mac(payload, secret)}`;
}

/** What `seal` stored, or null when the value is missing, altered or expired. */
export function unseal<T extends object>(sealed: string | undefined, secret: string, now = Date.now()): T | null {
  if (!sealed) return null;
  const [payload, signature, extra] = sealed.split(".");
  if (!payload || !signature || extra !== undefined) return null;
  const expected = Buffer.from(mac(payload, secret));
  const given = Buffer.from(signature);
  if (expected.length !== given.length || !timingSafeEqual(expected, given)) return null;
  let value: (T & { exp?: unknown }) | null;
  try {
    value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || typeof value.exp !== "number" || value.exp * 1000 <= now) return null;
  return value;
}

export type ConnectMode = "install" | "existing";

/** The state cookie's content. `state` is what GitHub has to send back. */
export interface ConnectState {
  state: string;
  organizationId: string;
  userId: string;
  mode: ConnectMode;
}

export function newState(): string {
  return randomBytes(24).toString("base64url");
}

/** The state cookie's content when the callback belongs to the round trip this browser started, else null. */
export function matchState(sealed: string | undefined, returned: string | null, userId: string, secret: string, now = Date.now()) {
  const value = unseal<ConnectState>(sealed, secret, now);
  if (!value || !returned || value.userId !== userId) return null;
  const a = Buffer.from(value.state);
  const b = Buffer.from(returned);
  return a.length === b.length && timingSafeEqual(a, b) ? value : null;
}

/** Install the App, with our state (GitHub sends it back only to installs that start here). */
export function installUrl(config: GitHubAppConfig, state: string): string {
  return `${GITHUB_WEB}/apps/${encodeURIComponent(config.slug)}/installations/new?state=${encodeURIComponent(state)}`;
}

/** GitHub's plain OAuth web flow, for installations that already exist. Same Callback URL. */
export function authorizeUrl(config: GitHubAppConfig, state: string, redirectUri: string): string {
  const query = new URLSearchParams({ client_id: config.clientId, state, redirect_uri: redirectUri });
  return `${GITHUB_WEB}/login/oauth/authorize?${query}`;
}

// ---------------------------------------------------------------------------
// GitHub's API, with the user access token from the callback.

export interface GitHubAccount {
  id: number;
  login: string;
  /** "User" or "Organization". */
  type: string;
}

export interface UserInstallation {
  id: number;
  account: GitHubAccount;
}

export class GitHubError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

type Fetch = typeof fetch;

export class GitHubClient {
  private readonly config: GitHubAppConfig;
  private readonly fetchImpl: Fetch;

  constructor(config: GitHubAppConfig, fetchImpl: Fetch = fetch) {
    this.config = config;
    this.fetchImpl = fetchImpl;
  }

  private async api<T>(token: string, path: string): Promise<T> {
    const response = await this.fetchImpl(`${GITHUB_API}${path}`, {
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "user-agent": "ember-web",
        "x-github-api-version": "2022-11-28",
      },
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) throw new GitHubError(`GitHub answered ${response.status} to GET ${path.split("?")[0]}`, response.status);
    return (await response.json()) as T;
  }

  /** Trade the callback's one-time `code` for a user access token. */
  async exchangeCode(code: string, redirectUri: string): Promise<string> {
    const response = await this.fetchImpl(`${GITHUB_WEB}/login/oauth/access_token`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json", "user-agent": "ember-web" },
      body: JSON.stringify({ client_id: this.config.clientId, client_secret: this.config.clientSecret, code, redirect_uri: redirectUri }),
      signal: AbortSignal.timeout(10_000),
    });
    const body = (await response.json().catch(() => null)) as { access_token?: string; error?: string } | null;
    if (!response.ok || !body?.access_token) {
      throw new GitHubError(`GitHub refused the code: ${body?.error ?? response.status}`, response.status);
    }
    return body.access_token;
  }

  /** The signed-in GitHub user. */
  async user(token: string): Promise<GitHubAccount> {
    return this.api<GitHubAccount>(token, "/user");
  }

  /** Every installation of this App the user can access, through any organization they belong to. */
  async installations(token: string): Promise<UserInstallation[]> {
    const all: UserInstallation[] = [];
    // 100 per page; 10 pages is far more than one person sees of one App
    for (let page = 1; page <= 10; page++) {
      const body = await this.api<{ total_count: number; installations: UserInstallation[] }>(
        token,
        `/user/installations?per_page=100&page=${page}`,
      );
      all.push(...body.installations);
      if (body.installations.length < 100 || all.length >= body.total_count) break;
    }
    return all;
  }

  /**
   * The user's role in an organization: "admin" for an owner, "member", or
   * null when they aren't an active member. Throws GitHubError 403 when the
   * App lacks the organization Members (read) permission.
   */
  async organizationRole(token: string, org: string): Promise<"admin" | "member" | null> {
    try {
      const membership = await this.api<{ state: string; role: string }>(token, `/user/memberships/orgs/${encodeURIComponent(org)}`);
      if (membership.state !== "active") return null;
      return membership.role === "admin" ? "admin" : "member";
    } catch (error) {
      if (error instanceof GitHubError && error.status === 404) return null;
      throw error;
    }
  }

  /** Revoke the user token. Best effort: it expires on its own (8 hours) if this fails. */
  async revoke(token: string): Promise<void> {
    try {
      await this.fetchImpl(`${GITHUB_API}/applications/${encodeURIComponent(this.config.clientId)}/token`, {
        method: "DELETE",
        headers: {
          accept: "application/vnd.github+json",
          authorization: `Basic ${Buffer.from(`${this.config.clientId}:${this.config.clientSecret}`).toString("base64")}`,
          "content-type": "application/json",
          "user-agent": "ember-web",
          "x-github-api-version": "2022-11-28",
        },
        body: JSON.stringify({ access_token: token }),
        signal: AbortSignal.timeout(10_000),
      });
    } catch {
      // the token lapses anyway
    }
  }
}

/** Run `use` with a user token from `code`, and revoke the token afterwards whatever happens. */
export async function withUserToken<T>(client: GitHubClient, code: string, redirectUri: string, use: (token: string) => Promise<T>): Promise<T> {
  const token = await client.exchangeCode(code, redirectUri);
  try {
    return await use(token);
  } finally {
    await client.revoke(token);
  }
}

/** The installation the redirect named, if the user can access it. Null means: not theirs. */
export async function verifyInstallation(client: GitHubClient, token: string, installationId: string): Promise<UserInstallation | null> {
  if (!/^[0-9]{1,20}$/.test(installationId)) return null;
  const installations = await client.installations(token);
  return installations.find((i) => String(i.id) === installationId) ?? null;
}

/** One row of the existing-installation picker. */
export interface Candidate {
  installationId: string;
  accountId: string;
  login: string;
  type: string;
  /** Why it can't be connected, or null when it can. */
  blocked: string | null;
}

/**
 * The installations the user can connect to an organization: those they
 * administer. Being a member is enough for GitHub to list an installation, so
 * this also checks that the user is the account itself (a personal account)
 * or an owner of the organization.
 */
export async function candidateInstallations(client: GitHubClient, token: string): Promise<Candidate[]> {
  const [user, installations] = await Promise.all([client.user(token), client.installations(token)]);
  const candidates: Candidate[] = [];
  for (const { id, account } of installations) {
    let blocked: string | null = null;
    if (account.type === "Organization") {
      try {
        const role = await client.organizationRole(token, account.login);
        if (role !== "admin") blocked = `Only an owner of ${account.login} can connect it.`;
      } catch (error) {
        blocked =
          error instanceof GitHubError && error.status === 403
            ? "Ember can't check your role in this organization yet. The App needs the organization Members (read) permission."
            : `GitHub didn't answer for ${account.login}. Try again.`;
      }
    } else if (account.id !== user.id) {
      blocked = `Only ${account.login} can connect it.`;
    }
    candidates.push({ installationId: String(id), accountId: String(account.id), login: account.login, type: account.type, blocked });
  }
  return candidates;
}
