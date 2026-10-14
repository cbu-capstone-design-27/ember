"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { authClient } from "../lib/auth-client.ts";
import { DashboardIcon, GraphIcon, LogOutIcon, SettingsIcon } from "./icons.tsx";

const LINKS = [
  { href: "/dashboard", label: "Dashboard", Icon: DashboardIcon },
  { href: "/graph", label: "Knowledge graph", Icon: GraphIcon },
  { href: "/settings", label: "Settings", Icon: SettingsIcon },
];

export function NavLinks({ className, linkClassName }: { className?: string; linkClassName?: string }) {
  const pathname = usePathname();
  return (
    <nav className={className} aria-label="Main">
      {LINKS.map(({ href, label, Icon }) => {
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <Link key={href} href={href} className={linkClassName} aria-current={active ? "page" : undefined}>
            <Icon />
            <span>{label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

export function SignOutButton({ className = "btn btn-ghost btn-sm", label = "Log out" }: { className?: string; label?: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className={className}
      disabled={busy}
      title={label}
      onClick={async () => {
        setBusy(true);
        await authClient.signOut();
        router.replace("/login");
        router.refresh();
      }}
    >
      {busy ? <span className="spinner" aria-hidden="true" /> : <LogOutIcon />}
      <span>{label}</span>
    </button>
  );
}
