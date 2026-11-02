// Tenant ids (EMBER-124): an organization's slug becomes the tenant its data
// is filed under, so slugs follow the tenant rules. Plain functions, safe to
// import in the browser (the create form checks the slug as it's typed).

/** The ontology's subgraph_id rule, which every tenant id follows. */
export const TENANT_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
export const TENANT_MAX_LENGTH = 63;

/** Why a tenant id is unusable, or null when it is fine. */
export function tenantProblem(tenant: string): string | null {
  if (!tenant) return "Pick a slug.";
  if (tenant.length > TENANT_MAX_LENGTH) return `Use at most ${TENANT_MAX_LENGTH} characters.`;
  if (!TENANT_PATTERN.test(tenant)) {
    return "Use lowercase letters, digits and dashes, starting with a letter or digit.";
  }
  return null;
}

/** A slug suggestion from an organization's name: "Kestrel Labs!" -> "kestrel-labs". */
export function slugFromName(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, TENANT_MAX_LENGTH)
    .replace(/-+$/, "");
}
