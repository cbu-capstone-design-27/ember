// Where the app gets its workspace. Today: the preview snapshot in src/data,
// shifted to look current and cut down to the sources the user picked; the
// browser then replays the user's edits on top (components/preview/store.tsx).
// Later: the user's tenant in the knowledge graph, behind the same function.

import preview from "../../data/preview-workspace.json";
import type { SourceId } from "../sources.ts";
import { forSources } from "./model.ts";
import { rebase } from "./time.ts";
import type { Workspace } from "./types.ts";

export function loadPreviewWorkspace(sources: readonly SourceId[], now = Date.now()): { workspace: Workspace; now: number } {
  const snapshot = preview as unknown as Workspace;
  return { workspace: forSources(rebase(snapshot, snapshot.anchor, new Date(now)), sources), now };
}
