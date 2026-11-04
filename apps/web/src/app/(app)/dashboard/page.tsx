import type { Metadata } from "next";
import { loadDashboard } from "../../../lib/source-records.ts";
import { requireOrganization } from "../../../lib/session.ts";
import { ingestionPool } from "../../connect/server.ts";
import { DashboardView, type DashboardState } from "./dashboard-view.tsx";

export const metadata: Metadata = { title: "Dashboard" };

export default async function DashboardPage() {
  const user = await requireOrganization("/dashboard");
  const { tenant, name } = user.organization;
  const pool = ingestionPool();
  let state: DashboardState = { kind: "unavailable" };
  if (pool) {
    try {
      state = { kind: "records", dashboard: await loadDashboard(pool, tenant) };
    } catch (error) {
      console.error(`[dashboard] reading source_records for tenant ${tenant} failed:`, error);
      state = { kind: "failed" };
    }
  }
  return <DashboardView organization={{ name, tenant }} picked={user.sources} state={state} now={Date.now()} />;
}
