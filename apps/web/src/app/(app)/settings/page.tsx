import type { Metadata } from "next";
import { headers } from "next/headers";
import { SourcesForm } from "../../../components/source-picker.tsx";
import { SignOutButton } from "../../../components/nav.tsx";
import { getAuth } from "../../../lib/auth.ts";
import { canInvite, invitationsOf, isEmberAdmin } from "../../../lib/organizations.ts";
import { requireOrganization } from "../../../lib/session.ts";
import { PasswordForm, PreviewDataForm, ProfileForm } from "./forms.tsx";
import { InvitationList, InviteForm, TenantLinkForm } from "./organization-forms.tsx";
import styles from "./settings.module.css";

export const metadata: Metadata = { title: "Settings" };

const DATE = new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
const SHORT_DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

export default async function SettingsPage() {
  const user = await requireOrganization("/settings");
  const org = user.organization;
  const auth = getAuth();
  const requestHeaders = await headers();
  const inviter = canInvite(org.role);
  const [{ members }, invitations] = await Promise.all([
    auth.api.listMembers({ query: { organizationId: org.id }, headers: requestHeaders }),
    inviter ? invitationsOf(auth, org.id, requestHeaders) : Promise.resolve([]),
  ]);
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
