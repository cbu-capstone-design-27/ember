import type { Metadata } from "next";
import { headers } from "next/headers";
import { SourcesForm } from "../../../components/source-picker.tsx";
import { SignOutButton } from "../../../components/nav.tsx";
import { getAuth } from "../../../lib/auth.ts";
import { listConnections, type Connection } from "../../../lib/connections.ts";
import { githubAppConfig, GITHUB_WEB } from "../../../lib/github-connect.ts";
import { canConnectSources, canInvite, invitationsOf, isEmberAdmin } from "../../../lib/organizations.ts";
import { ingestionPool } from "../../connect/github/server.ts";
import { requireOrganization } from "../../../lib/session.ts";
import { DisconnectForm } from "./connection-forms.tsx";
import { PasswordForm, PreviewDataForm, ProfileForm } from "./forms.tsx";
import { InvitationList, InviteForm, TenantLinkForm } from "./organization-forms.tsx";
import styles from "./settings.module.css";

export const metadata: Metadata = { title: "Settings" };

const DATE = new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
const SHORT_DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

/** What the Connect GitHub routes report back (connect/github, `?github=`). `{account}` is the GitHub login. */
const GITHUB_NOTICES: Record<string, { error: boolean; text: string }> = {
  connected: { error: false, text: "Connected {account}. Deliveries GitHub already sent for it are on their way to the graph." },
  requested: {
    error: false,
    text: "GitHub sent your install request to an owner of that organization. Once they approve it, come back and use Connect an existing installation.",
  },
  installed: {
    error: false,
    text: "The App was installed from GitHub's own page, so Ember can't tell which organization it is for. Use Connect an existing installation to pick it.",
  },
  expired: { error: true, text: "That GitHub sign-in expired, or started in another browser. Start again." },
  cancelled: { error: true, text: "GitHub sign-in was cancelled." },
  "not-yours": { error: true, text: "GitHub doesn't list that installation for your account, so it wasn't connected." },
  "other-tenant": { error: true, text: "{account} is connected to another Ember organization. An account can't move between organizations." },
  forbidden: { error: true, text: "Only owners and admins of this organization can connect sources." },
  unavailable: { error: true, text: "Connecting GitHub isn't set up on this server." },
  none: { error: true, text: "No installation was selected, so nothing was connected." },
  error: { error: true, text: "GitHub didn't answer as expected. Try again." },
};

function githubNotice(params: { github?: string | string[]; account?: string | string[] }) {
  const key = typeof params.github === "string" ? params.github : undefined;
  const notice = key ? GITHUB_NOTICES[key] : undefined;
  if (!notice) return null;
  // GitHub logins only, comma-separated: the URL is the browser's, so it can't put other text here
  const logins = typeof params.account === "string" ? params.account.split(", ") : [];
  const valid = logins.length > 0 && logins.every((login) => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(login));
  const account = valid ? logins.join(", ") : "the account";
  return { error: notice.error, text: notice.text.replace("{account}", account) };
}

const SOURCE_NAMES: Record<string, string> = { github: "GitHub", jira: "Jira", slack: "Slack", gitlab: "GitLab", teams: "Teams" };

function sourceName(source: string): string {
  return SOURCE_NAMES[source] ?? source;
}

/** The connection's name in the list: its label, or what the account id says. */
function connectionName(c: Connection): string {
  return c.label ?? (c.source === "github" ? `Installation ${c.accountId}` : c.accountId);
}

/** What the Disconnect action reports back (connection-actions.ts, `?disconnect=`). */
const DISCONNECT_NOTICES: Record<string, string> = {
  forbidden: "Only owners and admins of this organization can disconnect sources.",
  "not-found": "That connection isn't part of this organization.",
  unavailable: "Disconnecting isn't set up on this server.",
  error: "That request was incomplete. Try again.",
};

interface DisconnectNotice {
  error: boolean;
  text: string;
  /** Where to finish on the source's side, when Ember can't remove its hook itself. */
  link?: { href: string; text: string };
}

/**
 * The notice after a disconnect. The account is looked up among this
 * tenant's own connections, so the URL can't put other text on the page.
 */
function disconnectNotice(params: SettingsParams, connections: Connection[], githubSlug: string): DisconnectNotice | null {
  const key = typeof params.disconnect === "string" ? params.disconnect : undefined;
  if (!key) return null;
  if (key !== "done") return DISCONNECT_NOTICES[key] ? { error: true, text: DISCONNECT_NOTICES[key] } : null;
  const c = connections.find((x) => x.source === params.source && x.accountId === params.account);
  if (!c?.disconnectedAt) return null;
  const name = connectionName(c);
  if (c.source === "github") {
    return {
      error: false,
      text: `Disconnected ${name}. Ember stopped ingesting it. The ember-ingest App is still installed, so GitHub keeps sending events. Ember stores them without assigning them to an organization. To stop them, uninstall the App: choose ${name}, then Uninstall.`,
      link: { href: `${GITHUB_WEB}/apps/${githubSlug}/installations/new`, text: "Uninstall on GitHub" },
    };
  }
  return {
    error: false,
    text: `Disconnected ${name}. Ember stopped ingesting it and deleted its stored credentials. If ${sourceName(c.source)} still has a webhook pointing at Ember, remove it there.`,
  };
}

/** Who connected it: a member's name, or who it was when they've left. */
function connectedByName(c: Connection, names: Map<string, string>): string {
  if (c.connectedBy === null) return "an Ember operator";
  return names.get(c.connectedBy) ?? "a former member";
}

type SettingsParams = {
  github?: string | string[];
  account?: string | string[];
  disconnect?: string | string[];
  source?: string | string[];
};

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<SettingsParams>;
}) {
  const user = await requireOrganization("/settings");
  const org = user.organization;
  const auth = getAuth();
  const requestHeaders = await headers();
  const inviter = canInvite(org.role);
  const pool = ingestionPool();
  const [{ members }, invitations, connections] = await Promise.all([
    auth.api.listMembers({ query: { organizationId: org.id }, headers: requestHeaders }),
    inviter ? invitationsOf(auth, org.id, requestHeaders) : Promise.resolve([]),
    pool ? listConnections(pool, org.tenant) : Promise.resolve([] as Connection[]),
  ]);
  const params = await searchParams;
  const githubApp = githubAppConfig();
  const notice: DisconnectNotice | null = githubNotice(params) ?? disconnectNotice(params, connections, githubApp?.slug ?? "ember-ingest");
  const canConnect = canConnectSources(org.role);
  const connectable = pool !== null && githubApp !== null;
  const memberNames = new Map(members.map((m) => [m.user.id, m.user.name]));
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Settings</h1>
      </header>

      <section className={styles.section} aria-labelledby="profile">
        <div className={styles.aside}>
          <h2 id="profile">Profile</h2>
        </div>
        <ProfileForm name={user.name} email={user.email} memberSince={DATE.format(user.createdAt)} />
      </section>

      <section id="organization" className={styles.section} aria-labelledby="organization-title">
        <div className={styles.aside}>
          <h2 id="organization-title">Organization</h2>
        </div>
        <dl className={styles.facts}>
          <div>
            <dt>Name</dt>
            <dd>{org.name}</dd>
          </div>
          <div>
            <dt>Slug</dt>
            <dd>
              <code>{org.slug}</code>
            </dd>
          </div>
          <div>
            <dt>Tenant</dt>
            <dd>
              <code>{org.tenant}</code>
            </dd>
          </div>
          <div>
            <dt>Your role</dt>
            <dd>{org.role}</dd>
          </div>
        </dl>
      </section>

      <section id="connections" className={styles.section} aria-labelledby="connections-title">
        <div className={styles.aside}>
          <h2 id="connections-title">Connections</h2>
        </div>
        {notice && (
          <p className={`alert ${notice.error ? "alert-error" : ""} ${styles.notice}`} role={notice.error ? "alert" : "status"}>
            {notice.text}
            {notice.link && (
              <>
                {" "}
                <a href={notice.link.href} target="_blank" rel="noreferrer">
                  {notice.link.text}
                </a>
              </>
            )}
          </p>
        )}
        {connections.length > 0 ? (
          <ul className={styles.list} role="list">
            {connections.map((c) => (
              <li key={`${c.source}:${c.accountId}`} className={styles.listRow}>
                <div className={styles.listText}>
                  <b>{connectionName(c)}</b>
                  <span>
                    {sourceName(c.source)}
                    {c.source === "github" ? ` · installation ${c.accountId}` : c.label && c.label !== c.accountId ? ` · ${c.accountId}` : ""} · connected{" "}
                    {SHORT_DATE.format(c.addedAt)} by {connectedByName(c, memberNames)}
                    {c.disconnectedAt && ` · disconnected ${SHORT_DATE.format(c.disconnectedAt)}`}
                  </span>
                </div>
                <div className={styles.listActions}>
                  {c.disconnectedAt ? <span className="badge badge-danger">disconnected</span> : <span className="badge">connected</span>}
                  {canConnect && !c.disconnectedAt && (
                    <DisconnectForm organizationId={org.id} source={c.source} accountId={c.accountId} name={connectionName(c)} />
                  )}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.empty}>No source is connected to {org.name}.</p>
        )}
        <div className={styles.connectActions}>
          {!canConnect ? (
            <span className="field-hint">Owners and admins connect and disconnect sources.</span>
          ) : !connectable ? (
            <span className="field-hint">
              Connecting GitHub needs {pool ? "the App's client ID and secret (GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET)" : "the Postgres database ingestion uses"}.
            </span>
          ) : (
            <>
              <span className="field-hint">Installs the ember-ingest GitHub App on the repositories you choose.</span>
              <a className="btn btn-secondary" href="/connect/github?existing=1">
                Connect an existing GitHub installation
              </a>
              <a className="btn btn-primary" href="/connect/github">
                Connect GitHub
              </a>
            </>
          )}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="members">
        <div className={styles.aside}>
          <h2 id="members">Members</h2>
        </div>
        <ul className={styles.list} role="list">
          {members.map((member) => (
            <li key={member.id} className={styles.listRow}>
              <div className={styles.listText}>
                <b>{member.user.name}</b>
                <span>{member.user.email}</span>
              </div>
              <span className="badge">{member.role}</span>
            </li>
          ))}
        </ul>
      </section>

      {inviter && (
        <section className={styles.section} aria-labelledby="invite">
          <div className={styles.aside}>
            <h2 id="invite">Invite people</h2>
          </div>
          <InviteForm organizationId={org.id} />
          <InvitationList
            invitations={invitations.map((i) => ({ id: i.id, email: i.email, role: i.role, expires: SHORT_DATE.format(i.expiresAt) }))}
          />
        </section>
      )}

      {isEmberAdmin(user.email) && (
        <section className={styles.section} aria-labelledby="tenant">
          <div className={styles.aside}>
            <h2 id="tenant">Linked tenant</h2>
          </div>
          <TenantLinkForm organizationId={org.id} tenant={org.tenant} />
        </section>
      )}

      <section className={styles.section} aria-labelledby="sources">
        <div className={styles.aside}>
          <h2 id="sources">Sources</h2>
        </div>
        <div className={styles.sources}>
          <SourcesForm initial={user.sources} mode="settings" />
        </div>
      </section>

      <section id="preview-data" className={styles.section} aria-labelledby="preview">
        <div className={styles.aside}>
          <h2 id="preview">Preview data</h2>
        </div>
        <PreviewDataForm />
      </section>

      <section className={styles.section} aria-labelledby="security">
        <div className={styles.aside}>
          <h2 id="security">Password</h2>
        </div>
        <PasswordForm />
      </section>

      <section className={styles.section} aria-labelledby="session">
        <div className={styles.aside}>
          <h2 id="session">Session</h2>
        </div>
        <div className={styles.inline}>
          <SignOutButton className="btn btn-secondary" />
        </div>
      </section>
    </div>
  );
}
