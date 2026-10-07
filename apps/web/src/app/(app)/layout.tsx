// The signed-in app: sidebar on wide screens, top bar on narrow ones.
// Every page under (app) needs a user who has finished onboarding.

import Link from "next/link";
import type { ReactNode } from "react";
import { Avatar } from "../../components/avatar.tsx";
import { Wordmark } from "../../components/brand.tsx";
import { NavLinks, SignOutButton } from "../../components/nav.tsx";
import { PreviewProvider } from "../../components/preview/store.tsx";
import { ThemeSwitcher } from "../../components/theme.tsx";
import { requireOnboardedUser } from "../../lib/session.ts";
import { loadPreviewWorkspace } from "../../lib/workspace/load.ts";
import styles from "./shell.module.css";

export default async function AppLayout({ children }: { children: ReactNode }) {
  const user = await requireOnboardedUser();
  const { workspace, now } = loadPreviewWorkspace(user.sources);
  return (
    <PreviewProvider snapshot={workspace} serverNow={now} me={{ id: user.id, name: user.name, email: user.email }}>
      <div className={styles.shell}>
        <aside className={styles.sidebar}>
          <Link href="/dashboard" className={styles.brand} aria-label="Ember home">
            <Wordmark size={26} />
          </Link>

          <div className={styles.workspace}>
            <span className={styles.workspaceIcon} aria-hidden="true">K</span>
            <span className={styles.workspaceText}>
              <span className={styles.workspaceName}>Kestrel Labs</span>
              <span className={styles.workspaceMeta}>Preview workspace</span>
            </span>
          </div>

          <NavLinks className={styles.nav} linkClassName={styles.navLink} />

          <div className={styles.footer}>
            <ThemeSwitcher />
            <div className={styles.user}>
              <Avatar name={user.name} />
              <span className={styles.userText}>
                <span className={styles.userName}>{user.name}</span>
                <span className={styles.userEmail}>{user.email}</span>
              </span>
            </div>
            <SignOutButton className={`btn btn-ghost btn-sm ${styles.signOut}`} />
          </div>
        </aside>
        <main className={styles.main}>{children}</main>
      </div>
    </PreviewProvider>
  );
}
