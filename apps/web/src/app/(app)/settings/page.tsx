import type { Metadata } from "next";
import { SourcesForm } from "../../../components/source-picker.tsx";
import { SignOutButton } from "../../../components/nav.tsx";
import { requireOnboardedUser } from "../../../lib/session.ts";
import { PasswordForm, PreviewDataForm, ProfileForm } from "./forms.tsx";
import styles from "./settings.module.css";

export const metadata: Metadata = { title: "Settings" };

const DATE = new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });

export default async function SettingsPage() {
  const user = await requireOnboardedUser("/settings");
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
