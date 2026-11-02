"use client";

// The sidebar's organization menu (EMBER-124): which organization the app is
// showing, the others the user belongs to, and the way to create or join one.
// Switching sets the session's active organization, so it persists.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { authClient } from "../lib/auth-client.ts";
import { CheckIcon, ChevronRightIcon } from "./icons.tsx";
import styles from "./org-switcher.module.css";

export interface SwitcherOrg {
  id: string;
  name: string;
  slug: string;
}

export function OrgSwitcher({ organizations, activeId }: { organizations: SwitcherOrg[]; activeId: string }) {
  const router = useRouter();
  const menu = useRef<HTMLDetailsElement>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const active = organizations.find((o) => o.id === activeId) ?? organizations[0];

  async function switchTo(id: string) {
    if (id === activeId) return menu.current?.removeAttribute("open");
    setBusy(id);
    const { error } = await authClient.organization.setActive({ organizationId: id });
    setBusy(null);
    menu.current?.removeAttribute("open");
    if (!error) router.refresh();
  }

  return (
    <details ref={menu} className={styles.switcher}>
      <summary className={styles.summary} aria-label={`Organization: ${active?.name}. Switch organization`}>
        <span className={styles.name}>{active?.name}</span>
        <code>{active?.slug}</code>
        <ChevronRightIcon />
      </summary>
      <div className={styles.menu}>
        <ul role="list">
          {organizations.map((org) => (
            <li key={org.id}>
              <button
                type="button"
                className={styles.item}
                aria-current={org.id === activeId ? "true" : undefined}
                disabled={busy !== null}
                onClick={() => switchTo(org.id)}
              >
                <span className={styles.name}>{org.name}</span>
                {busy === org.id ? <span className="spinner" aria-hidden="true" /> : org.id === activeId && <CheckIcon />}
              </button>
            </li>
          ))}
        </ul>
        <Link href="/organizations" className={styles.item}>
          Create or join an organization
        </Link>
      </div>
    </details>
  );
}
