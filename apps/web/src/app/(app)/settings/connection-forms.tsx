"use client";

// The Disconnect button on Settings, Connections (EMBER-147). It asks first,
// then posts to the server action, which checks the caller's role again.

import { useFormStatus } from "react-dom";
import { disconnectConnection } from "./connection-actions.ts";

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn-ghost btn-sm" disabled={pending}>
      {pending && <span className="spinner" aria-hidden="true" />}
      Disconnect
    </button>
  );
}

export function DisconnectForm({ organizationId, source, accountId, name }: { organizationId: string; source: string; accountId: string; name: string }) {
  return (
    <form
      action={disconnectConnection}
      onSubmit={(event) => {
        const ok = window.confirm(
          `Disconnect ${name}? Ember stops ingesting it and deletes the credentials it stored. What it already ingested stays.`,
        );
        if (!ok) event.preventDefault();
      }}
    >
      <input type="hidden" name="organizationId" value={organizationId} />
      <input type="hidden" name="source" value={source} />
      <input type="hidden" name="accountId" value={accountId} />
      <SubmitButton />
    </form>
  );
}
