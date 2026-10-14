import type { Metadata } from "next";
import { requireOnboardedUser } from "../../../lib/session.ts";
import { DashboardView } from "./dashboard-view.tsx";

export const metadata: Metadata = { title: "Dashboard" };

export default async function DashboardPage() {
  const user = await requireOnboardedUser("/dashboard");
  return <DashboardView firstName={user.name.split(/\s+/)[0] || user.name} sources={user.sources} />;
}
