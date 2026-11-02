"use client";

// Settings → Organization (EMBER-124): invite people, see and revoke open
// invites, and (Ember admins only) link the organization to an existing tenant.
// Invites aren't emailed yet: the inviter copies the link and sends it.

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { authClient } from "../../../lib/auth-client.ts";
import { authErrorMessage } from "../../../lib/auth-errors.ts";
import { tenantProblem, TENANT_MAX_LENGTH } from "../../../lib/tenant.ts";
import { linkOrganizationTenant } from "./actions.ts";
import styles from "./settings.module.css";

function inviteLink(id: string) {
  return `${window.location.origin}/invite/${id}`;
}

/** Copies the link where the browser allows it (the clipboard needs HTTPS or localhost). */
function CopyLinkButton({ invitationId }: { invitationId: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(inviteLink(invitationId));
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        } catch {
          window.prompt("Copy the invite link:", inviteLink(invitationId));
        }
      }}
    >
      {copied ? "Copied" : "Copy link"}
    </button>
  );
}

export function InviteForm({ organizationId }: { organizationId: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"member" | "admin">("member");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [link, setLink] = useState<{ email: string; url: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setLink(null);
    const invited = email.trim();
    const { data, error } = await authClient.organization.inviteMember({ email: invited, role, organizationId });
    setBusy(false);
    if (error || !data) return setError(authErrorMessage(error, "Couldn't create the invite. Try again."));
    setLink({ email: invited, url: inviteLink(data.id) });
    setEmail("");
    router.refresh();
  }

  return (
    <form className={styles.form} onSubmit={onSubmit}>
      <div className={styles.inviteRow}>
        <label className="field">
          <span className="field-label">Email</span>
          <input
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="teammate@example.com"
            autoComplete="off"
            required
          />
        </label>
        <label className="field">
          <span className="field-label">Role</span>
          <select className="input" value={role} onChange={(e) => setRole(e.target.value as "member" | "admin")}>
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </select>
        </label>
      </div>
      {error && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}
      {link && (
        <label className="field" role="status">
          <span className="field-label">Send this link to {link.email}</span>
          <input className="input" value={link.url} readOnly onFocus={(e) => e.target.select()} />
          <span className="field-hint">
            Ember doesn&apos;t email invites yet. The link works for a week, and only for {link.email}. It also shows up
            when they log in.
          </span>
        </label>
      )}
      <div className={styles.formActions}>
        <span className="field-hint">Admins can invite people too. Members can&apos;t.</span>
        <button className="btn btn-primary" type="submit" disabled={busy || !email.trim()}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Create invite
        </button>
      </div>
    </form>
  );
}

export interface OpenInvitation {
  id: string;
  email: string;
  role: string;
  expires: string;
}

export function InvitationList({ invitations }: { invitations: OpenInvitation[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function revoke(invitationId: string) {
    setBusy(invitationId);
    setError(null);
    const { error } = await authClient.organization.cancelInvitation({ invitationId });
    setBusy(null);
    if (error) return setError(authErrorMessage(error, "Couldn't revoke the invite."));
    router.refresh();
  }

  if (invitations.length === 0) return <p className={styles.empty}>No open invites.</p>;
  return (
    <>
      {error && (
        <p className={`alert alert-error ${styles.listAlert}`} role="alert">
          {error}
        </p>
      )}
      <ul className={styles.list} role="list">
        {invitations.map((invite) => (
          <li key={invite.id} className={styles.listRow}>
            <div className={styles.listText}>
              <b>{invite.email}</b>
              <span>
                {invite.role} · expires {invite.expires}
              </span>
            </div>
            <div className={styles.listActions}>
              <CopyLinkButton invitationId={invite.id} />
              <button type="button" className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => revoke(invite.id)}>
                {busy === invite.id && <span className="spinner" aria-hidden="true" />}
                Revoke
              </button>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

export function TenantLinkForm({ organizationId, tenant }: { organizationId: string; tenant: string }) {
  const router = useRouter();
  const [value, setValue] = useState(tenant);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const problem = value ? tenantProblem(value) : null;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setStatus(null);
    const result = await linkOrganizationTenant(organizationId, value);
    setBusy(false);
    if (!result.ok) return setStatus({ kind: "error", text: result.error });
    setStatus({ kind: "success", text: `Linked to ${value}.` });
    router.refresh();
  }

  return (
    <form className={styles.form} onSubmit={onSubmit}>
      <label className="field">
        <span className="field-label">Tenant</span>
        <input
          className="input"
          value={value}
          onChange={(e) => setValue(e.target.value.trim())}
          maxLength={TENANT_MAX_LENGTH}
          spellCheck={false}
          required
          aria-invalid={problem ? true : undefined}
          aria-describedby="tenant-hint"
        />
        <span id="tenant-hint" className="field-hint">
          {problem ??
            "Which data this organization sees, such as ember-development. Each tenant belongs to one organization. Only Ember admins see this."}
        </span>
      </label>
      {status && (
        <p className={`alert ${status.kind === "error" ? "alert-error" : "alert-success"}`} role={status.kind === "error" ? "alert" : "status"}>
          {status.text}
        </p>
      )}
      <div className={styles.formActions}>
        <button className="btn btn-primary" type="submit" disabled={busy || !value || value === tenant || problem !== null}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Link tenant
        </button>
      </div>
    </form>
  );
}
