// Connect Jira, step 3 (EMBER-131, ADR 0010): the sites the owner's consent
// covers, each with its projects. Ticking projects and connecting one site
// registers a webhook for those projects and maps the site to the
// organization. The consent waits server side for fifteen minutes.

import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import { loadPending, PICK_COOKIE, unseal, type PendingConsent, type PickState } from "../../../../../lib/jira-connect.ts";
import { requireOrganization } from "../../../../../lib/session.ts";
import { cookieSecret } from "../../../../connect/server.ts";
import { apiKeyRing, verificationStore } from "../../../../connect/jira/server.ts";
import styles from "../../../settings/settings.module.css";
import { connectPickedSite } from "./actions.ts";

export const metadata: Metadata = { title: "Connect Jira" };

export default async function PickJiraSitePage({ searchParams }: { searchParams: Promise<{ none?: string | string[] }> }) {
  const { none } = await searchParams;
  const user = await requireOrganization("/settings");
  const pick = unseal<PickState>((await cookies()).get(PICK_COOKIE)?.value, await cookieSecret());
  // The organization the round trip started in, which may not be the active one any more
  const organization = pick && pick.userId === user.id ? user.memberships.find((m) => m.id === pick.organizationId) : undefined;
  const ring = apiKeyRing();
  const consent: PendingConsent | null = organization && pick && ring ? await loadPending(await verificationStore(), ring, pick) : null;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Connect Jira</h1>
      </header>
      {!consent ? (
        <section className={styles.section} aria-labelledby="sites">
          <div className={styles.aside}>
            <h2 id="sites">Jira sites</h2>
          </div>
          <p className={styles.empty}>
            This consent expired. <a href="/connect/jira">Connect Jira again</a> to see your sites.
          </p>
        </section>
      ) : (
        consent.sites.map((site) => (
          <section key={site.cloudId} className={styles.section} aria-labelledby={`site-${site.cloudId}`}>
            <div className={styles.aside}>
              <h2 id={`site-${site.cloudId}`}>{site.name}</h2>
              <span className="field-hint">{site.host}</span>
            </div>
            {site.blocked ? (
              <p className={styles.empty}>{site.blocked}</p>
            ) : (
              <form action={connectPickedSite}>
                {none === site.cloudId && (
                  <p className={`alert alert-error ${styles.notice}`} role="alert">
                    Tick at least one project.
                  </p>
                )}
                <input type="hidden" name="site" value={site.cloudId} />
                <ul className={styles.list} role="list">
                  {site.projects.map((project) => (
                    <li key={project.id} className={styles.listRow}>
                      <label className={styles.checkRow}>
                        <input type="checkbox" name="project" value={project.id} />
                        <span className={styles.listText}>
                          <b>{project.name}</b>
                          <span>{project.key}</span>
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
                {site.more && <p className="field-hint">The site has more projects than this list shows.</p>}
                <div className={`${styles.inline} ${styles.formActions}`}>
                  <span className="field-hint">
                    Ember receives changes to the ticked projects as {consent.atlassian.displayName || "the Jira user who consented"}, and files them under{" "}
                    {organization?.name}.
                  </span>
                  <Link className="btn btn-ghost" href="/settings#connections">
                    Cancel
                  </Link>
                  <button className="btn btn-primary" type="submit">
                    Connect {site.host}
                  </button>
                </div>
              </form>
            )}
          </section>
        ))
      )}
    </div>
  );
}
