"use client";

// Connect GitLab's two steps (EMBER-149): connect the group with a read_api
// token, then paste the signing token GitLab generated for each webhook the
// owner adds. Neither token is shown again once submitted.

import Link from "next/link";
import { useActionState } from "react";
import styles from "../../settings/settings.module.css";
import { addWebhookSigningToken, connectGitLab, type ConnectGitLabState, type SigningTokenState } from "./actions.ts";

export interface Group {
  accountId: string;
  label: string;
}

interface Common {
  organizationId: string;
  organizationName: string;
  webhookUrl: string;
  /** services/gitlab-ingestion/receiver.py's RECOMMENDED_WEBHOOK_TRIGGERS, from the server. */
  triggers: readonly string[];
}

export function ConnectGitLabForm(props: Common) {
  const [state, action, pending] = useActionState(connectGitLab, { status: "idle" } as ConnectGitLabState);

  if (state.status === "connected") {
    return (
      <>
        <p className="alert" role="status">
          {state.reconnected ? "Reconnected" : "Connected"} <b>{state.label}</b> ({state.accountId}) to {props.organizationName}.
          {state.tokenKind === "personal" &&
            " It was connected with a personal access token, which shows only that its user can read the group. A group access token, which only the group's Owners can make, is stronger proof."}
          {state.expiresAt && ` The access token expires on ${state.expiresAt}: reconnect with a new one before then.`}
        </p>
        <WebhookSteps {...props} group={{ accountId: state.accountId, label: state.label }} count={null} />
      </>
    );
  }

  return (
    <form className={styles.form} action={action}>
      <input type="hidden" name="organizationId" value={props.organizationId} />
      <label className="field">
        <span className="field-label">Instance URL</span>
        <input
          className="input"
          name="instance"
          type="url"
          defaultValue={state.status === "error" ? state.instance : "https://gitlab.com"}
          required
          autoComplete="off"
        />
        <span className="field-hint">https://gitlab.com, or your self-managed instance&apos;s address.</span>
      </label>
      <label className="field">
        <span className="field-label">Top-level group</span>
        <input className="input" name="group" defaultValue={state.status === "error" ? state.group : ""} placeholder="acme" required autoComplete="off" />
        <span className="field-hint">Its path, as in gitlab.com/acme. Every project under it is filed under {props.organizationName}.</span>
      </label>
      <label className="field">
        <span className="field-label">Access token</span>
        <input className="input" name="token" type="password" required autoComplete="off" spellCheck={false} />
        <span className="field-hint">
          The read_api scope only, with the Reporter role or above. On Premium or Ultimate, a group access token (the group&apos;s Settings, Access
          tokens). On Free, a personal access token of a group member, ideally one kept for integrations.
        </span>
      </label>
      {state.status === "error" && (
        <p className="alert alert-error" role="alert">
          {state.text}
        </p>
      )}
      <div className={styles.formActions}>
        <Link className="btn btn-ghost" href="/settings#connections">
          Cancel
        </Link>
        <button className="btn btn-primary" type="submit" disabled={pending}>
          {pending && <span className="spinner" aria-hidden="true" />}
          Connect GitLab
        </button>
      </div>
    </form>
  );
}

/** Add the webhook in GitLab, then paste its signing token. Repeatable: on Free, once per project. */
export function WebhookSteps({ organizationId, webhookUrl, triggers, group, count }: Common & { group: Group; count: number | null }) {
  const [state, action, pending] = useActionState(addWebhookSigningToken, { status: "idle" } as SigningTokenState);
  const stored = state.status === "added" ? state.count : count;

  return (
    <div className={styles.form}>
      <ol className="field-hint">
        <li>
          In GitLab, open Settings, Webhooks, Add new webhook: on the group with Premium or Ultimate, or on each project to ingest on Free.
        </li>
        <li>
          URL: <code>{webhookUrl}</code>
        </li>
        <li>Under Signing token, select Generate signing token, and copy it: GitLab shows it once.</li>
        <li>Tick {triggers.join(", ")}. Leave the confidential triggers off, then add the webhook.</li>
        <li>Paste the signing token below. For another project&apos;s webhook, repeat.</li>
      </ol>
      {/* React resets the form once the action is done, so the token doesn't stay in the field */}
      <form className={styles.form} action={action}>
        <input type="hidden" name="organizationId" value={organizationId} />
        <input type="hidden" name="accountId" value={group.accountId} />
        <label className="field">
          <span className="field-label">Signing token for {group.label}</span>
          <input className="input" name="signingToken" type="password" placeholder="whsec_…" required autoComplete="off" spellCheck={false} />
          <span className="field-hint">
            {stored === null || stored === 0
              ? "Until one is added, Ember refuses this group's deliveries."
              : `Ember verifies this group's deliveries with ${stored} webhook${stored === 1 ? "'s" : "s'"} signing token${stored === 1 ? "" : "s"}.`}
          </span>
        </label>
        {state.status === "error" && (
          <p className="alert alert-error" role="alert">
            {state.text}
          </p>
        )}
        {state.status === "added" && (
          <p className="alert" role="status">
            {state.already ? "That webhook's token was already added." : "Added. To check it, choose Test, then Push events, on the webhook in GitLab: it should show a 200 from Ember."}
          </p>
        )}
        <div className={styles.formActions}>
          <Link className="btn btn-ghost" href="/settings#connections">
            Done
          </Link>
          <button className="btn btn-primary" type="submit" disabled={pending}>
            {pending && <span className="spinner" aria-hidden="true" />}
            Add signing token
          </button>
        </div>
      </form>
    </div>
  );
}
