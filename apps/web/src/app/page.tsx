"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { authClient } from "../lib/auth-client.ts";

export default function Home() {
  const router = useRouter();
  const { data: session, isPending } = authClient.useSession();
  const [busy, setBusy] = useState(false);

  async function logOut() {
    setBusy(true);
    await authClient.signOut();
    setBusy(false);
    router.refresh();
  }

  if (isPending) return <p className="muted">Loading…</p>;

  if (!session) {
    return (
      <>
        <h1>Ember</h1>
        <p className="muted">How team context becomes agent context.</p>
        <p>
          <Link href="/login">Log in</Link> or <Link href="/signup">create an account</Link>.
        </p>
      </>
    );
  }

  return (
    <>
      <h1>Ember</h1>
      <p>
        Signed in as <strong>{session.user.email}</strong>
      </p>
      <button type="button" onClick={logOut} disabled={busy}>
        {busy ? "Logging out…" : "Log out"}
      </button>
    </>
  );
}
