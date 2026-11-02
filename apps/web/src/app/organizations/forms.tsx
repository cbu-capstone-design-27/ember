"use client";

// The create-or-join screen's forms (EMBER-124), and the Accept and Decline
// buttons the /invite page shares.

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { authClient } from "../../lib/auth-client.ts";
import { authErrorMessage } from "../../lib/auth-errors.ts";
import { slugFromName, tenantProblem, TENANT_MAX_LENGTH } from "../../lib/tenant.ts";
import styles from "./organizations.module.css";

export function CreateOrganizationForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  // The slug follows the name until the user types one of their own
  const [slugEdited, setSlugEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slugError = slug ? tenantProblem(slug) : null;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { error } = await authClient.organization.create({ name: name.trim(), slug });
    if (error) {
      setBusy(false);
      setError(authErrorMessage(error, "Couldn't create the organization. Try again."));
      return;
    }
    // Creating it also makes it the active organization
    router.replace("/dashboard");
    router.refresh();
  }

  return (
    <form className={styles.form} onSubmit={onSubmit}>
      <label className="field">
        <span className="field-label">Name</span>
        <input
          className="input"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            if (!slugEdited) setSlug(slugFromName(e.target.value));
          }}
          placeholder="Kestrel Labs"
          required
          maxLength={120}
          autoFocus
        />
      </label>
      <label className="field">
        <span className="field-label">Slug</span>
        <input
          className="input"
          value={slug}
          onChange={(e) => {
            setSlugEdited(true);
            setSlug(e.target.value.toLowerCase());
          }}
          placeholder="kestrel-labs"
          required
          maxLength={TENANT_MAX_LENGTH}
          spellCheck={false}
          aria-invalid={slugError ? true : undefined}
          aria-describedby="slug-hint"
        />
        <span id="slug-hint" className="field-hint">
          {slugError ?? "Lowercase letters, digits and dashes. It names your organization's data, so it can't change later."}
        </span>
      </label>
      {error && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <button className="btn btn-primary" type="submit" disabled={busy || !name.trim() || !slug || slugError !== null}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Create organization
        </button>
      </div>
    </form>
  );
}

/** Accept or decline an invitation, then go to the dashboard (accepting makes it the active organization). */
export function InvitationActions({ invitationId }: { invitationId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState<"accept" | "decline" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(action: "accept" | "decline") {
    setBusy(action);
    setError(null);
    const { error } =
      action === "accept"
        ? await authClient.organization.acceptInvitation({ invitationId })
        : await authClient.organization.rejectInvitation({ invitationId });
    setBusy(null);
    if (error) return setError(authErrorMessage(error, "Couldn't answer the invite. Try again."));
    if (action === "accept") router.replace("/dashboard");
    router.refresh();
  }

  return (
    <div className={styles.decision}>
      {error && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}
      <button className="btn btn-secondary btn-sm" type="button" disabled={busy !== null} onClick={() => decide("decline")}>
        {busy === "decline" && <span className="spinner" aria-hidden="true" />}
        Decline
      </button>
      <button className="btn btn-primary btn-sm" type="button" disabled={busy !== null} onClick={() => decide("accept")}>
        {busy === "accept" && <span className="spinner" aria-hidden="true" />}
        Accept
      </button>
    </div>
  );
}

/** Make one of the user's organizations active and open its dashboard. */
export function OpenOrganizationButton({ organizationId }: { organizationId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="btn btn-secondary btn-sm"
      type="button"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        const { error } = await authClient.organization.setActive({ organizationId });
        if (error) return setBusy(false);
        router.replace("/dashboard");
        router.refresh();
      }}
    >
      {busy && <span className="spinner" aria-hidden="true" />}
      Open
    </button>
  );
}
