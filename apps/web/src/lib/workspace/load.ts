// Where the dashboard and the graph get their data. Today: the preview
// workspace in src/data, shifted to look current and cut down to the
// sources the user picked. Later: the user's tenant in the knowledge graph,
// behind the same function.

import preview from "../../data/preview-workspace.json";
import type { SourceId } from "../sources.ts";
import { findInsights } from "./insights.ts";
import { buildModel, forSources } from "./model.ts";
import { rebase } from "./time.ts";
import type { Workspace } from "./types.ts";

export function loadWorkspace(sources: readonly SourceId[], now = Date.now()) {
  const snapshot = preview as unknown as Workspace;
  const workspace = forSources(rebase(snapshot, snapshot.anchor, new Date(now)), sources);
  const model = buildModel(workspace);
  return { model, insights: findInsights(model, now), now, preview: true };
}
