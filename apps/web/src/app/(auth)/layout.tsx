// Log-in and sign-up: the form on one side, the fire and what Ember does on the other.

import Link from "next/link";
import type { ReactNode } from "react";
import { SourceLogo, Wordmark } from "../../components/brand.tsx";
import { ThemeSwitcher } from "../../components/theme.tsx";
import type { SourceId } from "../../lib/sources.ts";
import styles from "./auth.module.css";
import { Flame } from "./flame.tsx";

// The preview workspace's CHK-131, as each source reports it (same as the landing page).
const EXAMPLE: Array<{ source: SourceId; where: string; says: string; odd?: boolean }> = [
  { source: "slack", where: "#checkout-dev", says: "“Saved cards (CHK-131) is done!”" },
  { source: "github", where: "checkout-web#219", says: "Merged 5h ago" },
  { source: "jira", where: "CHK-131", says: "In Development", odd: true },
];

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.page}>
      <section className={styles.story} aria-hidden="true">
        <Flame />
        <Link href="/" className={`${styles.storyBrand} on-dark`} tabIndex={-1}>
          <Wordmark size={26} />
        </Link>
        <div className={styles.storyBody}>
          <p className={styles.storyTitle}>
            Slack says it&apos;s done. <em>Jira says it isn&apos;t.</em>
          </p>
          <ol className={styles.ledger}>
            {EXAMPLE.map((row) => (
              <li key={row.source} data-odd={row.odd}>
                <SourceLogo source={row.source} size={14} />
                <code>{row.where}</code>
                <span>{row.says}</span>
              </li>
            ))}
          </ol>
        </div>
        <p className={styles.storyFoot}>GitHub · GitLab · Jira · Slack · Microsoft Teams</p>
      </section>

      <section className={styles.formSide}>
        <div className={styles.topBar}>
          <Link href="/" className={styles.mobileBrand} aria-label="Ember home">
            <Wordmark size={22} />
          </Link>
          <ThemeSwitcher />
        </div>
        <div className={styles.formWrap}>{children}</div>
      </section>
    </div>
  );
}
