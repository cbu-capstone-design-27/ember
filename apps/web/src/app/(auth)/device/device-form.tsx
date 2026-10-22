"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { AlertIcon } from "../../../components/icons.tsx";
import { authClient } from "../../../lib/auth-client.ts";
import styles from "../auth.module.css";
import { decide } from "./actions.ts";

type Outcome = "approved" | "denied" | "expired" | "invalid" | "taken";

const MESSAGES: Record<Outcome, { title: string; text: string }> = {
  approved: {
    title: "The CLI is signed in",
    text: "You can close this tab and go back to your terminal.",
  },
  denied: {
    title: "Sign-in denied",
    text: "The CLI was not signed in. Run ember auth login again if you meant to approve it.",
  },
  expired: {
    title: "This code has expired",
    text: "Codes last 30 minutes. Run ember auth login again for a new one.",
  },
  invalid: {
    title: "We don't recognize that code",
    text: "Check the code in your terminal and enter it below, or run ember auth login again.",
  },
  taken: {
    title: "Another account has this code",
    text: "It was opened by a different account first. Run ember auth login again for a new code.",
  },
};

export function DeviceMessage({ state }: { state: Outcome }) {
  const { title, text } = MESSAGES[state];
  return (
    <>
      <div className={styles.heading}>
        <h1>{title}</h1>
        <p>{text}</p>
      </div>
      {state === "invalid" && <CodeInput />}
    </>
  );
}

/** For a visit to /device without a code: type the one the terminal shows. */
export function DeviceCodeForm() {
  return (
    <>
      <div className={styles.heading}>
        <h1>Sign in to the Ember CLI</h1>
        <p>Enter the code shown in your terminal after running ember auth login.</p>
      </div>
      <CodeInput />
    </>
  );
}

function CodeInput() {
  return (
    <form className={styles.form} method="get" action="/device">
      <label className="field">
        <span className="field-label">Code</span>
        <input className={`input ${styles.codeInput}`} name="user_code" placeholder="ABCD-EFGH" autoComplete="off" autoCapitalize="characters" spellCheck={false} required autoFocus />
      </label>
      <button type="submit" className="btn btn-primary btn-lg btn-block">
        Continue
      </button>
    </form>
  );
}

export function DeviceApproval({ userCode, code, email, here }: { userCode: string; code: string; email: string; here: string }) {
  const router = useRouter();
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<"approve" | "deny" | "switch" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const backHere = `/login?next=${encodeURIComponent(here)}`;

  async function onDecide(action: "approve" | "deny") {
    setBusy(action);
    setError(null);
    try {
      const { state } = await decide(userCode, action);
      if (state === "signed-out") return router.replace(backHere);
      if (state === "changed") return router.refresh(); // the page shows what happened instead
      setOutcome(state);
    } catch {
      setError("Couldn't reach Ember. Check your connection and try again.");
    } finally {
      setBusy(null);
    }
  }

  async function switchAccount() {
    setBusy("switch");
    await authClient.signOut();
    router.replace(backHere);
    router.refresh();
  }

  if (outcome) return <DeviceMessage state={outcome} />;
  return (
    <>
      <div className={styles.heading}>
        <h1>Sign in to the Ember CLI</h1>
        <p>
          Approve only if this code matches the one in your terminal. The CLI will be signed in as <b>{email}</b>.
        </p>
      </div>
      <p className={styles.deviceCode} aria-label="Code">
        {code}
      </p>
      <div className={styles.form}>
        {error && (
          <p className="alert alert-error" role="alert">
            <AlertIcon />
            <span>{error}</span>
          </p>
        )}
        <button type="button" className="btn btn-primary btn-lg btn-block" disabled={busy !== null} onClick={() => onDecide("approve")}>
          {busy === "approve" && <span className="spinner" aria-hidden="true" />}
          {busy === "approve" ? "Approving…" : "Approve"}
        </button>
        <button type="button" className="btn btn-secondary btn-lg btn-block" disabled={busy !== null} onClick={() => onDecide("deny")}>
          {busy === "deny" ? "Denying…" : "Deny"}
        </button>
      </div>
      <p className={styles.switch}>
        Not {email}?{" "}
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={switchAccount}>
          {busy === "switch" ? "Signing out…" : "Use another account"}
        </button>
      </p>
    </>
  );
}
