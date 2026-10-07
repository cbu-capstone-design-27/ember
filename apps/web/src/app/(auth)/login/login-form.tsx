"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { AlertIcon } from "../../../components/icons.tsx";
import { PasswordInput } from "../../../components/password-input.tsx";
import { authClient } from "../../../lib/auth-client.ts";
import { authErrorMessage } from "../../../lib/auth-errors.ts";
import styles from "../auth.module.css";

export function LoginForm({ next }: { next: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    const { error } = await authClient.signIn.email({
      email: String(form.get("email") ?? "").trim(),
      password: String(form.get("password") ?? ""),
    });
    if (error) {
      setBusy(false);
      setError(authErrorMessage(error, "Couldn't log in. Try again."));
      return;
    }
    router.replace(next);
    router.refresh();
  }

  return (
    <>
      <div className={styles.heading}>
        <h1>Welcome back</h1>
        <p>Log in to see what changed across your team&apos;s tools.</p>
      </div>
      <form className={styles.form} onSubmit={onSubmit}>
        <label className="field">
          <span className="field-label">Email</span>
          <input className="input" name="email" type="email" autoComplete="email" placeholder="you@company.com" required autoFocus />
        </label>
        <label className="field">
          <span className="field-label">Password</span>
          <PasswordInput name="password" autoComplete="current-password" required />
        </label>
        {error && (
          <p className="alert alert-error" role="alert">
            <AlertIcon />
            <span>{error}</span>
          </p>
        )}
        <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy ? "Logging in…" : "Log in"}
        </button>
      </form>
      <p className={styles.switch}>
        New to Ember?{" "}
        <Link className="link" href="/signup">
          Create an account
        </Link>
      </p>
    </>
  );
}
