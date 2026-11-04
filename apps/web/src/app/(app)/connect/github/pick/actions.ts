"use server";

// Connect GitHub, existing installations (EMBER-130, ADR 0010): connect the
// ones the owner ticked on the picker. Only installations the callback found
// and cleared are accepted; the signed picker cookie holds that list, so the
// browser can't add one.

import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { after } from "next/server";
import { connectAccount, handOffAgainLater } from "../../../../../lib/connections.ts";
import { COOKIE_PATH, PICK_COOKIE, unseal } from "../../../../../lib/github-connect.ts";
import { connector, cookieSecret, ingestionPool, type PickState } from "../../../../connect/github/server.ts";

function settings(notice: string, account?: string): never {
  const query = new URLSearchParams({ github: notice });
  if (account) query.set("account", account);
  redirect(`/settings?${query}#connections`);
}

export async function connectPicked(formData: FormData) {
  const jar = await cookies();
  const pick = unseal<PickState>(jar.get(PICK_COOKIE)?.value, await cookieSecret());
  if (!pick) settings("expired");
  const who = await connector(await headers(), pick.organizationId);
  if (!who.ok || who.value.userId !== pick.userId) settings("forbidden");
  const pool = ingestionPool();
  if (!pool) settings("unavailable");

  const wanted = new Set(formData.getAll("installation").map(String));
  const chosen = pick.candidates.filter((c) => c.blocked === null && wanted.has(c.installationId));
  if (chosen.length === 0) settings("none");

  const connected: string[] = [];
  let refused: string | undefined;
  for (const candidate of chosen) {
    const input = {
      source: "github",
      accountId: candidate.installationId,
      tenant: who.value.membership.tenant,
      label: candidate.login,
      externalId: candidate.accountId,
      userId: who.value.userId,
    };
    const result = await connectAccount(pool, input);
    if (!result.ok) {
      refused ??= candidate.login;
      continue;
    }
    connected.push(candidate.login);
    after(() => handOffAgainLater(pool, input));
  }
  jar.set(PICK_COOKIE, "", { path: COOKIE_PATH, maxAge: 0 });
  if (refused) settings("other-tenant", refused);
  settings("connected", connected.join(", "));
}
