import type { Metadata } from "next";
import { requireOnboardedUser } from "../../../lib/session.ts";
import { buildGraph } from "../../../lib/workspace/graph.ts";
import type { Severity } from "../../../lib/workspace/insights.ts";
import { loadWorkspace } from "../../../lib/workspace/load.ts";
import { GraphExplorer, type NodeFlag } from "./graph-explorer.tsx";

export const metadata: Metadata = { title: "Knowledge graph" };

const RANK: Record<Severity, number> = { high: 0, medium: 1, low: 2 };

export default async function GraphPage({ searchParams }: { searchParams: Promise<{ focus?: string | string[] }> }) {
  const user = await requireOnboardedUser("/graph");
  const { model, insights, now } = loadWorkspace(user.sources);
  const graph = buildGraph(model, now);

  // Insights mark the node they're about, so the graph shows where the trouble is.
  const flags: Record<string, NodeFlag> = {};
  for (const i of insights) {
    const f = (flags[i.subject] ??= { severity: i.severity, insights: [] });
    if (RANK[i.severity] < RANK[f.severity]) f.severity = i.severity;
    f.insights.push({ title: i.title, action: i.action, severity: i.severity });
  }

  const focus = (await searchParams).focus;
  return (
    <GraphExplorer
      graph={graph}
      flags={flags}
      focus={Array.isArray(focus) ? focus[0] : focus}
      tenant={model.ws.organization.tenant}
      organization={model.ws.organization.name}
    />
  );
}
