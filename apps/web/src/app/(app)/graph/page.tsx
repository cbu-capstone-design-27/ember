import type { Metadata } from "next";
import { requireOnboardedUser } from "../../../lib/session.ts";
import { GraphExplorer } from "./graph-explorer.tsx";

export const metadata: Metadata = { title: "Knowledge graph" };

export default async function GraphPage({ searchParams }: { searchParams: Promise<{ focus?: string | string[] }> }) {
  await requireOnboardedUser("/graph");
  const focus = (await searchParams).focus;
  return <GraphExplorer focus={Array.isArray(focus) ? focus[0] : focus} />;
}
