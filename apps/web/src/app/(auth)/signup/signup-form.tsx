"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { AlertIcon } from "../../../components/icons.tsx";
import { PasswordInput, passwordScore, SCORE_COLORS, SCORE_LABELS } from "../../../components/password-input.tsx";
import { authClient } from "../../../lib/auth-client.ts";
import { authErrorMessage } from "../../../lib/auth-errors.ts";
import styles from "../auth.module.css";
import { Steps } from "../steps.tsx";

const MIN_PASSWORD_LENGTH = 8; // matches lib/auth.ts

export function SignupForm() {
  const router = useRouter();
  const [error, setError] = useState<{ text: string; exists: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [password, setPassword] = useState("");
  const score = passwordScore(password, MIN_PASSWORD_LENGTH);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    const { error } = await authClient.signUp.email({
      name: String(form.get("name") ?? "").trim(),
      email: String(form.get("email") ?? "").trim(),
      password,
    });
    if (error) {
      setBusy(false);
      setError({
        text: authErrorMessage(error, "Couldn't create the account. Try again."),
        exists: error.code?.startsWith("USER_ALREADY_EXISTS") ?? false,
      });
      return;
    }
    // Signing up also signs in. Next: pick the sources.
    router.replace("/onboarding");
    router.refresh();
  }

  return (
    <>
      <Steps current={1} />
      <div className={styles.heading}>
        <h1>Create your account</h1>
        <p>Then tell us which tools your team uses. It takes a minute.</p>
      </div>
      <form className={styles.form} onSubmit={onSubmit}>
        <label className="field">
          <span className="field-label">Full name</span>
          <input className="input" name="name" autoComplete="name" placeholder="Ada Lovelace" required autoFocus maxLength={120} />
        </label>
        <label className="field">
          <span className="field-label">Work email</span>
          <input className="input" name="email" type="email" autoComplete="email" placeholder="you@company.com" required />
        </label>
        <div className="field">
          <label className="field-label" htmlFor="password">
            Password
          </label>
          <PasswordInput
            id="password"
            name="password"
            autoComplete="new-password"
            minLength={MIN_PASSWORD_LENGTH}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-describedby="password-hint"
          />
          <div className={styles.strength} aria-hidden={password.length === 0}>
            {[1, 2, 3, 4].map((n) => (
              <span key={n} className={styles.strengthBar} style={{ background: score >= n ? SCORE_COLORS[score] : undefined }} />
            ))}
            <span className={styles.strengthLabel}>{password ? SCORE_LABELS[score] : ""}</span>
          </div>
          <span id="password-hint" className="field-hint">
            At least {MIN_PASSWORD_LENGTH} characters. A longer passphrase is stronger than symbols.
          </span>
        </div>
        {error && (
          <p className="alert alert-error" role="alert">
            <AlertIcon />
            <span>
              {error.text}{" "}
              {error.exists && (
                <Link className="link" href="/login">
                  Log in instead
                </Link>
              )}
            </span>
          </p>
        )}
        <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy ? "Creating your account…" : "Continue"}
        </button>
      </form>
      <p className={styles.switch}>
        Already have an account?{" "}
        <Link className="link" href="/login">
          Log in
        </Link>
      </p>
    </>
  );
}
