// Log-in and sign-up: the form on one side, what Ember does on the other.

import Link from "next/link";
import type { ReactNode } from "react";
import { SourceLogo, Wordmark } from "../../components/brand.tsx";
import { ThemeSwitcher } from "../../components/theme.tsx";
import styles from "./auth.module.css";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.page}>
      <section className={styles.story} aria-hidden="true">
        <Link href="/" className={styles.storyBrand} tabIndex={-1}>
          <Wordmark size={30} />
        </Link>
        <div className={styles.storyBody}>
          <h2 className={styles.storyTitle}>Your team&apos;s context, connected.</h2>
          <p className={styles.storyText}>
            Ember reads your pull requests, tickets and conversations, links them into one knowledge graph, and
            tells you where they disagree.
          </p>
          <div className={styles.preview}>
            <div className={styles.previewHead}>
              <span className={styles.previewDot} />
              Status drift
            </div>
            <p className={styles.previewTitle}>CHK-131 looks done, but Jira says In Development</p>
            <ul className={styles.previewList}>
              <li>
                <SourceLogo source="slack" size={16} />
                <span>
                  <b>#checkout-dev</b> &ldquo;Saved cards is done! #219 merged&rdquo;
                </span>
              </li>
              <li>
                <SourceLogo source="github" size={16} />
                <span>
                  <b>checkout-web#219</b> Merged 5h ago
                </span>
              </li>
              <li>
                <SourceLogo source="jira" size={16} />
                <span>
                  <b>CHK-131</b> In Development
                </span>
              </li>
            </ul>
          </div>
        </div>
        <p className={styles.storyFoot}>GitHub · GitLab · Jira · Slack · Microsoft Teams</p>
      </section>

      <section className={styles.formSide}>
        <div className={styles.topBar}>
          <Link href="/" className={styles.mobileBrand} aria-label="Ember home">
            <Wordmark size={24} />
          </Link>
          <ThemeSwitcher />
        </div>
        <div className={styles.formWrap}>{children}</div>
      </section>
    </div>
  );
}
