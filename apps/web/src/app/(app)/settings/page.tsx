import type { Metadata } from "next";
import { headers } from "next/headers";
import { SourcesForm } from "../../../components/source-picker.tsx";
import { SignOutButton } from "../../../components/nav.tsx";
import { getAuth } from "../../../lib/auth.ts";
import { listConnections, type Connection } from "../../../lib/connections.ts";
import { githubAppConfig } from "../../../lib/github-connect.ts";
import { canConnectSources, canInvite, invitationsOf, isEmberAdmin } from "../../../lib/organizations.ts";
import { ingestionPool } from "../../connect/github/server.ts";
import { requireOrganization } from "../../../lib/session.ts";
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

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ github?: string | string[]; account?: string | string[] }>;
}) {
  const user = await requireOrganization("/settings");
  const org = user.organization;
  const auth = getAuth();
  const requestHeaders = await headers();
  const inviter = canInvite(org.role);
  const pool = ingestionPool();
  const [{ members }, invitations, github] = await Promise.all([
    auth.api.listMembers({ query: { organizationId: org.id }, headers: requestHeaders }),
    inviter ? invitationsOf(auth, org.id, requestHeaders) : Promise.resolve([]),
    pool ? listConnections(pool, org.tenant, "github") : Promise.resolve([] as Connection[]),
  ]);
  const notice = githubNotice(await searchParams);
  const canConnect = canConnectSources(org.role);
  const connectable = pool !== null && githubAppConfig() !== null;
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

      <section id="github" className={styles.section} aria-labelledby="github-title">
        <div className={styles.aside}>
          <h2 id="github-title">GitHub</h2>
        </div>
        {notice && (
          <p className={`alert ${notice.error ? "alert-error" : ""} ${styles.notice}`} role={notice.error ? "alert" : "status"}>
            {notice.text}
          </p>
        )}
        {github.length > 0 ? (
          <ul className={styles.list} role="list">
            {github.map((c) => (
              <li key={c.accountId} className={styles.listRow}>
                <div className={styles.listText}>
                  <b>{c.label ?? `Installation ${c.accountId}`}</b>
                  <span>
                    Installation {c.accountId} · connected {SHORT_DATE.format(c.addedAt)}
                  </span>
                </div>
                {c.disconnectedAt ? <span className="badge badge-danger">disconnected</span> : <span className="badge">connected</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.empty}>No GitHub account is connected to {org.name}.</p>
        )}
        <div className={styles.connectActions}>
          {!canConnect ? (
            <span className="field-hint">Owners and admins connect sources.</span>
          ) : !connectable ? (
            <span className="field-hint">
              Connecting GitHub needs {pool ? "the App's client ID and secret (GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET)" : "the Postgres database ingestion uses"}.
            </span>
          ) : (
            <>
              <span className="field-hint">Installs the ember-ingest GitHub App on the repositories you choose.</span>
              <a className="btn btn-secondary" href="/connect/github?existing=1">
                Connect an existing installation
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
