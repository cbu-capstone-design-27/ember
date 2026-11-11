// Connect GitLab (EMBER-149, ADR 0010). Without `?account=`, an owner or
// admin connects a top-level group with a pasted read_api token. With it, they
// add another webhook's signing token to a group the organization already
// connected (Settings links here from each GitLab connection).

import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { listConnections } from "../../../../lib/connections.ts";
import { gitlabConnectConfig, signingTokenCount, SOURCE, WEBHOOK_TRIGGERS } from "../../../../lib/gitlab-connect.ts";
import { canConnectSources } from "../../../../lib/organizations.ts";
import { requireOrganization } from "../../../../lib/session.ts";
import { ingestionPool, keyRing } from "../../../connect/server.ts";
import styles from "../../settings/settings.module.css";
import { ConnectGitLabForm, WebhookSteps } from "./form.tsx";

export const metadata: Metadata = { title: "Connect GitLab" };

export default async function ConnectGitLabPage({ searchParams }: { searchParams: Promise<{ account?: string | string[] }> }) {
  const { account } = await searchParams;
  const user = await requireOrganization("/connect/gitlab");
  const org = user.organization;
  const pool = ingestionPool();
  const config = gitlabConnectConfig();
  const webhook = keyRing("webhook");
  const ready = pool !== null && config !== null && keyRing("api") !== null && webhook !== null;
  const common = { organizationId: org.id, organizationName: org.name, webhookUrl: config?.webhookUrl ?? "", triggers: WEBHOOK_TRIGGERS };

  let body: ReactNode;
  if (!canConnectSources(org.role)) {
    body = <p className={styles.empty}>Only owners and admins of {org.name} can connect sources.</p>;
  } else if (!ready) {
    body = (
      <p className={styles.empty}>
        Connecting GitLab needs{" "}
        {pool ? "intake's address and both key rings (GITLAB_WEBHOOK_URL, SOURCE_CREDENTIALS_API_KEYS, SOURCE_CREDENTIALS_WEBHOOK_KEYS)" : "the Postgres database ingestion uses"}.
      </p>
    );
  } else if (typeof account === "string") {
    // Only a connected group of this organization: the URL is the browser's
    const connection = (await listConnections(pool, org.tenant, SOURCE)).find((c) => c.accountId === account && c.disconnectedAt === null);
    const count = connection ? await signingTokenCount(pool, webhook, account, org.tenant) : null;
    body =
      connection && count !== null ? (
        <WebhookSteps {...common} group={{ accountId: connection.accountId, label: connection.label ?? connection.accountId }} count={count} />
      ) : (
        <p className={styles.empty}>
          That group isn&apos;t connected to {org.name}. <Link href="/connect/gitlab">Connect GitLab</Link>.
        </p>
      );
  } else {
    body = <ConnectGitLabForm {...common} />;
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Connect GitLab</h1>
      </header>
      <section className={styles.section} aria-labelledby="gitlab">
        <div className={styles.aside}>
          <h2 id="gitlab">{typeof account === "string" ? "Add a webhook" : "Group"}</h2>
        </div>
        {body}
      </section>
    </div>
  );
}
