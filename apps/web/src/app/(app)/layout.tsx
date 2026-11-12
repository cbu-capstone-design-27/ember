// The signed-in app: sidebar on wide screens, top bar on narrow ones.
// Every page under (app) needs a user who has finished onboarding, and works in
// one organization (EMBER-124), their personal workspace at least (EMBER-165);
// the sidebar shows which one and switches.

import Link from "next/link";
import type { ReactNode } from "react";
import { Avatar } from "../../components/avatar.tsx";
import { SourceLogo, Wordmark } from "../../components/brand.tsx";
import { NavLinks, SignOutButton } from "../../components/nav.tsx";
import { OrgSwitcher } from "../../components/org-switcher.tsx";
import { PreviewProvider } from "../../components/preview/store.tsx";
import { requireOrganization } from "../../lib/session.ts";
import { sourceInfo } from "../../lib/sources.ts";
import { loadPreviewWorkspace } from "../../lib/workspace/load.ts";
import styles from "./shell.module.css";

export default async function AppLayout({ children }: { children: ReactNode }) {
  const user = await requireOrganization();
  const { workspace, now } = loadPreviewWorkspace(user.sources);
  return (
    <PreviewProvider snapshot={workspace} serverNow={now} me={{ id: user.id, name: user.name, email: user.email }}>
      <div className={styles.shell}>
        <aside className={styles.sidebar}>
          <Link href="/dashboard" className={styles.brand} aria-label="Ember home">
            <Wordmark size={20} />
          </Link>

          <div className={styles.workspace}>
            <OrgSwitcher
              organizations={user.memberships.map(({ id, name, slug, personal }) => ({ id, name, slug, personal }))}
              activeId={user.organization.id}
            />
          </div>

          <NavLinks className={styles.nav} linkClassName={styles.navLink} />

          <div className={styles.sources}>
            <Link href="/settings#sources" className={styles.sourcesHead}>
              Sources
            </Link>
            {user.sources.map((id) => (
              <Link key={id} href="/settings#sources" className={styles.navLink}>
                <SourceLogo source={id} size={14} />
                <span>{sourceInfo(id).name}</span>
              </Link>
            ))}
          </div>

          <div className={styles.footer}>
            <div className={styles.user} title={user.email}>
              <Avatar name={user.name} />
              <span className={styles.userName}>{user.name}</span>
            </div>
            <div className={styles.footerRow}>
              <SignOutButton className={`btn btn-ghost btn-sm ${styles.signOut}`} />
            </div>
          </div>
        </aside>
        <main className={styles.main}>{children}</main>
      </div>
    </PreviewProvider>
  );
}
