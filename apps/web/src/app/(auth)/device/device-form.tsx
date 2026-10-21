"use client";

import { useState } from "react";
import { AlertIcon } from "../../../components/icons.tsx";
import { authClient } from "../../../lib/auth-client.ts";
import styles from "../auth.module.css";

type Outcome = "approved" | "denied" | "expired" | "invalid";

const MESSAGES: Record<Outcome, { title: string; text: (email: string) => string }> = {
  approved: {
    title: "The CLI is signed in",
    text: (email) => `The Ember CLI is signed in as ${email}. You can close this tab and go back to your terminal.`,
  },
  denied: {
    title: "Sign-in denied",
    text: () => "The CLI was not signed in. Run ember auth login again if you meant to approve it.",
  },
  expired: {
    title: "This code has expired",
    text: () => "Codes last 30 minutes. Run ember auth login again for a new one.",
  },
  invalid: {
    title: "We don't recognize that code",
    text: () => "Check the code in your terminal and enter it below, or run ember auth login again.",
  },
};

export function DeviceMessage({ state, email }: { state: Outcome; email: string }) {
  const { title, text } = MESSAGES[state];
  return (
    <>
      <div className={styles.heading}>
        <h1>{title}</h1>
        <p>{text(email)}</p>
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

export function DeviceApproval({ userCode, code, email }: { userCode: string; code: string; email: string }) {
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(action: "approve" | "deny") {
    setBusy(action);
    setError(null);
    const { error } = action === "approve" ? await authClient.device.approve({ userCode }) : await authClient.device.deny({ userCode });
    setBusy(null);
    if (!error) return setOutcome(action === "approve" ? "approved" : "denied");
    const reason = (error as { error?: string; error_description?: string }).error;
    if (reason === "expired_token") return setOutcome("expired");
    setError((error as { error_description?: string }).error_description || "Couldn't finish signing in the CLI. Try again.");
  }

  if (outcome) return <DeviceMessage state={outcome} email={email} />;
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
        <button type="button" className="btn btn-primary btn-lg btn-block" disabled={busy !== null} onClick={() => decide("approve")}>
          {busy === "approve" && <span className="spinner" aria-hidden="true" />}
          {busy === "approve" ? "Approving…" : "Approve"}
        </button>
        <button type="button" className="btn btn-secondary btn-lg btn-block" disabled={busy !== null} onClick={() => decide("deny")}>
          {busy === "deny" ? "Denying…" : "Deny"}
        </button>
      </div>
    </>
  );
}
