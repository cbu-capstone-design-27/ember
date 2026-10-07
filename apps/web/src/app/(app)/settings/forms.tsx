"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { PasswordInput } from "../../../components/password-input.tsx";
import { authClient } from "../../../lib/auth-client.ts";
import { authErrorMessage } from "../../../lib/auth-errors.ts";
import styles from "./settings.module.css";

type Status = { kind: "error" | "success"; text: string } | null;

function StatusLine({ status }: { status: Status }) {
  if (!status) return null;
  return (
    <p className={`alert ${status.kind === "error" ? "alert-error" : "alert-success"}`} role={status.kind === "error" ? "alert" : "status"}>
      {status.text}
    </p>
  );
}

export function ProfileForm({ name, email }: { name: string; email: string }) {
  const router = useRouter();
  const [value, setValue] = useState(name);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setStatus(null);
    const { error } = await authClient.updateUser({ name: value.trim() });
    setBusy(false);
    if (error) return setStatus({ kind: "error", text: authErrorMessage(error, "Couldn't save your name.") });
    setStatus({ kind: "success", text: "Saved." });
    router.refresh();
  }

  return (
    <form className={styles.form} onSubmit={onSubmit}>
      <label className="field">
        <span className="field-label">Full name</span>
        <input className="input" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="name" required maxLength={120} />
      </label>
      <label className="field">
        <span className="field-label">Email</span>
        <input className="input" value={email} readOnly aria-describedby="email-hint" />
        <span id="email-hint" className="field-hint">
          Your sign-in email. Changing it comes with email verification.
        </span>
      </label>
      <StatusLine status={status} />
      <div className={styles.formActions}>
        <button className="btn btn-primary" type="submit" disabled={busy || value.trim() === name || !value.trim()}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Save profile
        </button>
      </div>
    </form>
  );
}

export function PasswordForm() {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formEl = event.currentTarget;
    const form = new FormData(formEl);
    const next = String(form.get("newPassword") ?? "");
    if (next !== String(form.get("confirm") ?? "")) {
      setStatus({ kind: "error", text: "The new passwords don't match." });
      return;
    }
    setBusy(true);
    setStatus(null);
    const { error } = await authClient.changePassword({
      currentPassword: String(form.get("currentPassword") ?? ""),
      newPassword: next,
      revokeOtherSessions: true,
    });
    setBusy(false);
    if (error) return setStatus({ kind: "error", text: authErrorMessage(error, "Couldn't change your password.") });
    formEl.reset();
    setStatus({ kind: "success", text: "Password changed. Other sessions were signed out." });
  }

  return (
    <form className={styles.form} onSubmit={onSubmit}>
      <label className="field">
        <span className="field-label">Current password</span>
        <PasswordInput name="currentPassword" autoComplete="current-password" required />
      </label>
      <div className={styles.twoCol}>
        <label className="field">
          <span className="field-label">New password</span>
          <PasswordInput name="newPassword" autoComplete="new-password" minLength={8} required />
        </label>
        <label className="field">
          <span className="field-label">Confirm new password</span>
          <PasswordInput name="confirm" autoComplete="new-password" minLength={8} required />
        </label>
      </div>
      <StatusLine status={status} />
      <div className={styles.formActions}>
        <button className="btn btn-secondary" type="submit" disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Change password
        </button>
      </div>
    </form>
  );
}
