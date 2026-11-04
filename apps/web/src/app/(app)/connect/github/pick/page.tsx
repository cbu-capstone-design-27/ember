// Connect GitHub, existing installations (EMBER-130, ADR 0010): after the
// owner signs in to GitHub, the callback lists the `ember-ingest`
// installations they can see and stores them in a signed cookie for ten
// minutes. This page shows them; the ones they administer and nobody has
// connected can be ticked and connected.

import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import { PICK_COOKIE, unseal } from "../../../../../lib/github-connect.ts";
import { requireOrganization } from "../../../../../lib/session.ts";
import { cookieSecret, type PickState } from "../../../../connect/github/server.ts";
import styles from "../../../settings/settings.module.css";
import { connectPicked } from "./actions.ts";

export const metadata: Metadata = { title: "Connect GitHub" };

export default async function PickInstallationsPage() {
  const user = await requireOrganization("/settings");
  const pick = unseal<PickState>((await cookies()).get(PICK_COOKIE)?.value, await cookieSecret());
  // The organization the round trip started in, which may not be the active one any more
  const organization = pick && pick.userId === user.id ? user.memberships.find((m) => m.id === pick.organizationId) : undefined;
  const usable = organization ? pick : null;
  const open = usable?.candidates.filter((c) => c.blocked === null) ?? [];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Connect GitHub</h1>
      </header>
      <section className={styles.section} aria-labelledby="installations">
        <div className={styles.aside}>
          <h2 id="installations">Your ember-ingest installations</h2>
        </div>
        {!usable ? (
          <p className={styles.empty}>
            This list expired. <a href="/connect/github?existing=1">Sign in to GitHub again</a> to see your installations.
          </p>
        ) : usable.candidates.length === 0 ? (
          <p className={styles.empty}>
            GitHub lists no installations of the App for your account. <a href="/connect/github">Install it</a> instead.
          </p>
        ) : (
          <form action={connectPicked}>
            <ul className={styles.list} role="list">
              {usable.candidates.map((c) => (
                <li key={c.installationId} className={styles.listRow}>
                  <label className={styles.checkRow}>
                    <input type="checkbox" name="installation" value={c.installationId} disabled={c.blocked !== null} />
                    <span className={styles.listText}>
                      <b>{c.login}</b>
                      <span>{c.blocked ?? `${c.type === "Organization" ? "Organization" : "Personal account"} · installation ${c.installationId}`}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <div className={`${styles.inline} ${styles.formActions}`}>
              <span className="field-hint">Connecting files its data under {organization?.name}.</span>
              <Link className="btn btn-ghost" href="/settings#github">
                Cancel
              </Link>
              <button className="btn btn-primary" type="submit" disabled={open.length === 0}>
                Connect selected
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
