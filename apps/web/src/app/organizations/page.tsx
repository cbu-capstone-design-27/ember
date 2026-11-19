// Create or join an organization (EMBER-124), where the sidebar's menu leads
// to make or join another one. Invites addressed to the user's email wait here.
// Every user already has a personal workspace (EMBER-165), so nobody is sent
// here for lack of one.

import type { Metadata } from "next";
import Link from "next/link";
import { Wordmark } from "../../components/brand.tsx";
import { SignOutButton } from "../../components/nav.tsx";
import { getAuth } from "../../lib/auth.ts";
import { invitationsFor } from "../../lib/organizations.ts";
import { getCurrentOrganizations, requireUser } from "../../lib/session.ts";
import shell from "../onboarding/onboarding.module.css";
import { CreateOrganizationForm, InvitationActions, OpenOrganizationButton } from "./forms.tsx";
import styles from "./organizations.module.css";

export const metadata: Metadata = { title: "Organizations" };

const DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

export default async function OrganizationsPage() {
  const user = await requireUser("/organizations");
  const [{ memberships }, invitations] = await Promise.all([getCurrentOrganizations(), invitationsFor(getAuth(), user.email)]);

  return (
    <div className={shell.page}>
      <header className={shell.header}>
        <Link href="/" aria-label="Ember home" className={shell.brand}>
          <Wordmark size={20} />
        </Link>
        <div className={shell.headerActions}>
          <Link href="/dashboard" className="btn btn-ghost btn-sm">
            Back to the dashboard
          </Link>
          <SignOutButton />
        </div>
      </header>
      <main className={shell.main}>
        <div className={shell.intro}>
          <h1>Organizations</h1>
          <p>
            An organization is a project your team works on together: its members see the same tickets, pull requests and
            discussions. You can belong to several.
          </p>
        </div>

        <div className={styles.panels}>
          {invitations.length > 0 && (
            <section className={styles.panel} aria-labelledby="invites">
              <div className={styles.panelHead}>
                <h2 id="invites">Invitations</h2>
              </div>
              <ul className={styles.rows} role="list">
                {invitations.map((invite) => (
                  <li key={invite.id} className={styles.row}>
                    <div className={styles.rowText}>
                      <b>{invite.organizationName ?? "An organization"}</b>
                      <span>
                        As {invite.role}. Expires {DATE.format(invite.expiresAt)}.
                      </span>
                    </div>
                    <InvitationActions invitationId={invite.id} />
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className={styles.panel} aria-labelledby="yours">
            <div className={styles.panelHead}>
              <h2 id="yours">Your organizations</h2>
            </div>
            <ul className={styles.rows} role="list">
              {memberships.map((org) => (
                <li key={org.id} className={styles.row}>
                  <div className={styles.rowText}>
                    <b>{org.name}</b>
                    <span>
                      {org.slug} · {org.role}
                    </span>
                  </div>
                  <OpenOrganizationButton organizationId={org.id} />
                </li>
              ))}
            </ul>
          </section>

          <section className={styles.panel} aria-labelledby="create">
            <div className={styles.panelHead}>
              <h2 id="create">Create an organization</h2>
            </div>
            <CreateOrganizationForm />
          </section>

          <section className={styles.panel} aria-labelledby="join">
            <div className={styles.panelHead}>
              <h2 id="join">Join an organization</h2>
            </div>
            <p className={styles.note}>
              Ask one of its owners or admins to invite <b>{user.email}</b>. The invite shows up here, or open the link
              they send you.
            </p>
          </section>
        </div>
      </main>
    </div>
  );
}
