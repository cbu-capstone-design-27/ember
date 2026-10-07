"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { authClient } from "../../lib/auth-client.ts";

export default function SignUp() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    const { error } = await authClient.signUp.email({
      name: String(form.get("name") ?? ""),
      email: String(form.get("email") ?? ""),
      password: String(form.get("password") ?? ""),
    });
    setBusy(false);
    if (error) {
      setError(error.message ?? "Could not create the account.");
      return;
    }
    router.push("/"); // signing up also signs in
  }

  return (
    <>
      <h1>Create an account</h1>
      <form onSubmit={onSubmit}>
        <label>
          Name
          <input name="name" autoComplete="name" required />
        </label>
        <label>
          Email
          <input name="email" type="email" autoComplete="email" required />
        </label>
        <label>
          Password (at least 8 characters)
          <input name="password" type="password" autoComplete="new-password" minLength={8} required />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "Creating…" : "Sign up"}
        </button>
      </form>
      <p className="muted">
        Already have an account? <Link href="/login">Log in</Link>
      </p>
    </>
  );
}
