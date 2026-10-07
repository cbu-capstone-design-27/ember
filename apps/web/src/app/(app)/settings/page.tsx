import type { Metadata } from "next";
import { SourcesForm } from "../../../components/source-picker.tsx";
import { ThemeSwitcher } from "../../../components/theme.tsx";
import { SignOutButton } from "../../../components/nav.tsx";
import { requireOnboardedUser } from "../../../lib/session.ts";
import { PasswordForm, ProfileForm } from "./forms.tsx";
import styles from "./settings.module.css";

export const metadata: Metadata = { title: "Settings" };

const DATE = new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });

export default async function SettingsPage() {
  const user = await requireOnboardedUser("/settings");
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Settings</h1>
        <p className="muted">Your profile, the sources Ember reads, and how the app looks.</p>
      </header>

      <section className={styles.section} aria-labelledby="profile">
        <div className={styles.aside}>
          <h2 id="profile">Profile</h2>
          <p>How your name appears to your team. Member since {DATE.format(user.createdAt)}.</p>
        </div>
        <div className="card">
          <ProfileForm name={user.name} email={user.email} />
        </div>
      </section>

      <section className={styles.section} aria-labelledby="sources">
        <div className={styles.aside}>
          <h2 id="sources">Sources</h2>
          <p>The tools your team works in. The dashboard and the graph only show these.</p>
        </div>
        <SourcesForm initial={user.sources} mode="settings" />
      </section>

      <section className={styles.section} aria-labelledby="appearance">
        <div className={styles.aside}>
          <h2 id="appearance">Appearance</h2>
          <p>Light, dark, or follow your system. Saved in this browser.</p>
        </div>
        <div className={`card ${styles.cardPad}`}>
          <ThemeSwitcher showLabels />
        </div>
      </section>

      <section className={styles.section} aria-labelledby="security">
        <div className={styles.aside}>
          <h2 id="security">Password</h2>
          <p>Changing it signs you out everywhere else.</p>
        </div>
        <div className="card">
          <PasswordForm />
        </div>
      </section>

      <section className={styles.section} aria-labelledby="session">
        <div className={styles.aside}>
          <h2 id="session">Session</h2>
          <p>Log out of Ember in this browser.</p>
        </div>
        <div className={`card ${styles.cardPad}`}>
          <SignOutButton className="btn btn-secondary" />
        </div>
      </section>
    </div>
  );
}
