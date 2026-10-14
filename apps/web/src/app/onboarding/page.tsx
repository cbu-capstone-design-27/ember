// Sign-up step 2: which tools the team uses. Also where a user lands if they
// signed up but never picked sources (or, later, signed in with GitHub).

import type { Metadata } from "next";
import Link from "next/link";
import { Wordmark } from "../../components/brand.tsx";
import { SignOutButton } from "../../components/nav.tsx";
import { SourcesForm } from "../../components/source-picker.tsx";
import { ThemeSwitcher } from "../../components/theme.tsx";
import { requireUser } from "../../lib/session.ts";
import { Steps } from "../(auth)/steps.tsx";
import styles from "./onboarding.module.css";

export const metadata: Metadata = { title: "Pick your sources" };

export default async function OnboardingPage() {
  const user = await requireUser("/onboarding");
  const firstName = user.name.split(/\s+/)[0] || user.name;
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link href="/" aria-label="Ember home" className={styles.brand}>
          <Wordmark size={26} />
        </Link>
        <div className={styles.headerActions}>
          <ThemeSwitcher />
          <SignOutButton />
        </div>
      </header>
      <main className={styles.main}>
        <Steps current={2} />
        <div className={styles.intro}>
          <h1>Which tools does your team use, {firstName}?</h1>
          <p>
            Ember builds its knowledge graph from these. Pick every one your team works in. You can change this
            anytime in Settings.
          </p>
          <p className={styles.note}>
            Nothing connects yet. GitHub, Jira and Slack come with a preview workspace, so you can explore the
            dashboard and the graph right away.
          </p>
        </div>
        <SourcesForm initial={user.sources} mode="onboarding" />
      </main>
    </div>
  );
}
