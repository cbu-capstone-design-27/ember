"use client";

// The landing's slice of the knowledge graph: CHK-131 in the middle, its
// neighbours on an inner ring, one more step out on an outer ring (layout in
// data.ts). Source records show their real logo, people their initials.
// Labels only on key nodes, on the side away from their edge; hover any node
// to light its links and read it.
// The rings settle in once on screen; with reduced motion they're just there.

import { useEffect, useMemo, useRef, useState } from "react";
import { initials } from "../avatar.tsx";
import { SourceLogo } from "../brand.tsx";
import type { MiniGraph } from "./data.ts";
import { useInView, useReducedMotion } from "./in-view.tsx";
import styles from "./graph.module.css";

/** Types that are labelled without a hover. */
const LABELLED = new Set(["WorkItem", "Change", "Person", "Container", "Decision"]);

const short = (label: string) => (label.length > 26 ? `${label.slice(0, 25)}…` : label);

export function MiniGraphView({ graph }: { graph: MiniGraph }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, "0px");
  const reduced = useReducedMotion();
  const [settled, setSettled] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  // Settle once, the first time the graph comes into view.
  useEffect(() => {
    if (inView) setSettled(true);
  }, [inView]);

  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
  const lit = useMemo(() => {
    if (!hover) return null;
    const set = new Set([hover]);
    for (const e of graph.edges) {
      if (e.source === hover) set.add(e.target);
      if (e.target === hover) set.add(e.source);
    }
    return set;
  }, [hover, graph]);

  return (
    <div
      ref={ref}
      className={styles.graph}
      data-settled={settled || reduced}
      role="img"
      aria-label={`CHK-131 and ${graph.nodes.length - 1} records linked to it in the preview knowledge graph`}
      onPointerLeave={() => setHover(null)}
    >
      <svg className={styles.edges} viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {graph.edges.map((e) => {
          const a = byId.get(e.source)!;
          const b = byId.get(e.target)!;
          const on = !!lit && lit.has(e.source) && lit.has(e.target) && (e.source === hover || e.target === hover);
          return (
            <line
              key={e.id}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              vectorEffect="non-scaling-stroke"
              data-focus={e.focus}
              data-outer={a.depth === 2 || b.depth === 2 || undefined}
              data-lit={on || undefined}
              data-dim={(!!lit && !on) || undefined}
            />
          );
        })}
      </svg>
      {graph.nodes.map((n, i) => {
        const showLabel = n.depth === 0 || (n.depth === 1 && LABELLED.has(n.type)) || n.id === hover;
        return (
          <div
            key={n.id}
            className={styles.node}
            data-depth={n.depth}
            data-person={n.source === null || undefined}
            data-up={n.up || undefined}
            data-up-alone={n.upAlone || undefined}
            data-dim={(!!lit && !lit.has(n.id)) || undefined}
            style={{ left: `${n.x}%`, top: `${n.y}%`, ["--delay" as string]: `${n.depth * 220 + i * 25}ms` }}
            onPointerEnter={() => setHover(n.id)}
          >
            <span className={styles.dot}>
              {n.source ? <SourceLogo source={n.source} size={n.depth === 0 ? 22 : n.depth === 1 ? 16 : 13} /> : <span>{initials(n.label)}</span>}
            </span>
            {showLabel && <span className={styles.label}>{n.depth === 0 ? n.label : short(n.label)}</span>}
          </div>
        );
      })}
    </div>
  );
}
