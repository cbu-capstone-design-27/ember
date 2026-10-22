"use client";

// What the fire panel shows above the flames: the drift on log-in (and the
// CLI's /device page), the sources linking up on sign-up.

import { usePathname } from "next/navigation";
import { ConnectStoryView } from "../../components/story/connect.tsx";
import type { ConnectStory, Drift } from "../../components/story/data.ts";
import { DriftStory } from "../../components/story/drift.tsx";

export function AuthStory({ drift, connect }: { drift: Drift; connect: ConnectStory }) {
  const pathname = usePathname();
  return pathname.startsWith("/signup") ? <ConnectStoryView story={connect} /> : <DriftStory drift={drift} />;
}
