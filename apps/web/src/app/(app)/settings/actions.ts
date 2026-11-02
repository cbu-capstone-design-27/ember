"use server";

// Linking an organization to an existing tenant (EMBER-124), run on the server
// with the visitor's session. linkTenant checks they're an Ember admin.
import { headers } from "next/headers";
import { getAuth } from "../../../lib/auth.ts";
import { linkTenant, type LinkResult, type OrgAdapter } from "../../../lib/organizations.ts";

export async function linkOrganizationTenant(organizationId: string, tenant: string): Promise<LinkResult> {
  // A server action is a public endpoint: check what the browser sent.
  if (typeof organizationId !== "string" || typeof tenant !== "string" || !organizationId) {
    throw new Error("linkOrganizationTenant needs an organization id and a tenant");
  }
  const auth = getAuth();
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session) return { ok: false, error: "Your session ended. Log in again." };
  const { adapter } = await auth.$context;
  return linkTenant(adapter as unknown as OrgAdapter, {
    actorEmail: session.user.email,
    organizationId,
    tenant: tenant.trim(),
  });
}
