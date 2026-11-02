// An invite link (EMBER-124): /invite/<invitation id>, copied by an owner or
// admin from Settings and sent by hand (there is no invite email yet). The
// visitor logs in or signs up first, then accepts or declines. Only the
// account with the invited email can.

import { isAPIError } from "better-auth/api";
import type { Metadata } from "next";
import { headers } from "next/headers";
import Link from "next/link";
import { Wordmark } from "../../../components/brand.tsx";
import { SignOutButton } from "../../../components/nav.tsx";
import { getAuth } from "../../../lib/auth.ts";
import { requireUser } from "../../../lib/session.ts";
import shell from "../../onboarding/onboarding.module.css";
import { InvitationActions } from "../../organizations/forms.tsx";
import styles from "../../organizations/organizations.module.css";

export const metadata: Metadata = { title: "Invitation" };

type Found =
  | { state: "open"; organizationName: string; inviterEmail: string; role: string }
  | { state: "not-yours" }
  | { state: "gone" };

async function findInvitation(id: string): Promise<Found> {
  try {
    const invite = await getAuth().api.getInvitation({ query: { id }, headers: await headers() });
    return { state: "open", organizationName: invite.organizationName, inviterEmail: invite.inviterEmail, role: invite.role };
  } catch (error) {
    if (!isAPIError(error)) throw error;
    return error.statusCode === 403 ? { state: "not-yours" } : { state: "gone" };
  }
}

export default async function InvitePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const user = await requireUser(`/invite/${encodeURIComponent(id)}`);
  const invite = await findInvitation(id);

  return (
    <div className={shell.page}>
      <header className={shell.header}>
        <Link href="/" aria-label="Ember home" className={shell.brand}>
          <Wordmark size={20} />
        </Link>
        <div className={shell.headerActions}>
          <SignOutButton />
        </div>
      </header>
      <main className={shell.main}>
        {invite.state === "open" ? (
          <>
            <div className={shell.intro}>
              <h1>Join {invite.organizationName}</h1>
              <p>
                {invite.inviterEmail} invited you to join as {invite.role === "admin" ? "an admin" : `a ${invite.role}`}.
              </p>
            </div>
            <InvitationActions invitationId={id} />
          </>
        ) : invite.state === "not-yours" ? (
          <div className={shell.intro}>
            <h1>This invite is for someone else</h1>
            <p>
              You&apos;re logged in as <b>{user.email}</b>, and the invite was sent to a different email. Log out and log in
              with the invited one.
            </p>
          </div>
        ) : (
          <div className={shell.intro}>
            <h1>This invite no longer works</h1>
            <p>
              It was revoked, has expired, or was already used. Ask whoever sent it for a new one, or see{" "}
              <Link className="link" href="/organizations">
                your organizations
              </Link>
              .
            </p>
          </div>
        )}
      </main>
    </div>
  );
}
