"use client";

// The sign-up panel: each source links into one graph, one at a time.
// GitLab has no preview data and Teams has no ingestion yet; both stay
// unlinked and say so.

import { useEffect, useRef, useState } from "react";
import { EmberMark, SourceLogo } from "../brand.tsx";
import type { ConnectStory } from "./data.ts";
import { useInView, useReducedMotion } from "./in-view.tsx";
import styles from "./story.module.css";

// Tile centres on a 440×400 canvas, around the hub at (220, 196).
const AT: Record<string, { x: number; y: number }> = {
  github: { x: 220, y: 40 },
  gitlab: { x: 370, y: 140 },
  jira: { x: 320, y: 320 },
  slack: { x: 120, y: 320 },
  teams: { x: 70, y: 140 },
};
const HUB = { x: 220, y: 196 };
const STEP_MS = 1300;
const HOLD_MS = 4500;

export function ConnectStoryView({ story, className }: { story: ConnectStory; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref);
  const reduced = useReducedMotion();
  const linked = story.sources.filter((s) => s.status === "linked");
  const last = linked.length + 1;
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (reduced || !inView) return;
    const timer = setTimeout(() => setStep((s) => (s >= last ? 0 : s + 1)), step >= last ? HOLD_MS : STEP_MS);
    return () => clearTimeout(timer);
  }, [step, last, inView, reduced]);

  const shown = reduced ? last : step;
  const order = new Map(linked.map((s, i) => [s.id, i + 1]));
  return (
    <div ref={ref} className={`${styles.connect} ${className ?? ""}`} aria-hidden="true">
      <svg className={styles.links} viewBox="0 0 440 400">
        {story.sources.map((s) => {
          const p = AT[s.id];
          const n = order.get(s.id);
          return (
            <line
              key={s.id}
              className={n ? styles.link : styles.linkIdle}
              data-on={n ? shown >= n : true}
              x1={p.x}
              y1={p.y}
              x2={HUB.x}
              y2={HUB.y}
              pathLength={1}
            />
          );
        })}
      </svg>
      {story.sources.map((s) => {
        const p = AT[s.id];
        const n = order.get(s.id);
        return (
          <div key={s.id} className={styles.tile} style={{ left: p.x, top: p.y }} data-on={n ? shown >= n : false} data-idle={!n}>
            <SourceLogo source={s.id} size={20} />
            <code>{s.status === "linked" ? s.detail : s.status}</code>
          </div>
        );
      })}
      <div className={styles.hub} style={{ left: HUB.x, top: HUB.y }} data-on={shown >= last}>
        <EmberMark size={30} />
        <code>
          {story.tenant} · {story.nodes} nodes · {story.edges} edges
        </code>
      </div>
    </div>
  );
}
