"use client";

// A small, live slice of the knowledge graph for the landing page: CHK-131
// and what it touches. d3-force lays it out once; drag a node and the rest
// follow, hover one to light its links. Nothing moves on its own.

import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationLinkDatum, type SimulationNodeDatum } from "d3-force";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { TYPE_COLOR } from "../node-style.ts";
import type { MiniGraph } from "./data.ts";
import styles from "./story.module.css";

interface Node extends SimulationNodeDatum {
  id: string;
  r: number;
}

// The drawing size; the SVG scales to its box, so text stays near its CSS size at full width.
const W = 1160;
const H = 460;
const round = (v: number | undefined) => Math.round((v ?? 0) * 10) / 10;
const short = (label: string) => (label.length > 22 ? `${label.slice(0, 21)}…` : label);

export function MiniGraphView({ graph }: { graph: MiniGraph }) {
  const svg = useRef<SVGSVGElement>(null);
  const [, redraw] = useState(0);
  const [hover, setHover] = useState<string | null>(null);
  const drag = useRef<Node | null>(null);

  const sim = useMemo(() => {
    const nodes: Node[] = graph.nodes.map((n, i) => ({
      id: n.id,
      r: n.focus ? 14 : n.type === "Person" ? 10 : 7,
      x: W / 2 + Math.cos(i) * 200,
      y: H / 2 + Math.sin(i) * 120,
    }));
    const links: Array<SimulationLinkDatum<Node> & { id: string }> = graph.edges.map((e) => ({ id: e.id, source: e.source, target: e.target }));
    const simulation = forceSimulation(nodes)
      .force("link", forceLink<Node, SimulationLinkDatum<Node>>(links).id((d) => d.id).distance(110).strength(0.5))
      .force("charge", forceManyBody().strength(-480))
      .force("collide", forceCollide<Node>((d) => d.r + 22))
      .force("x", forceX(W / 2).strength(0.03))
      .force("y", forceY(H / 2).strength(0.16))
      .stop();
    for (let i = 0; i < 300; i++) simulation.tick();
    return { simulation, nodes, links, byId: new Map(nodes.map((n) => [n.id, n])) };
  }, [graph]);

  useEffect(() => {
    const s = sim.simulation;
    s.on("tick", () => redraw((v) => v + 1));
    return () => {
      s.on("tick", null);
      s.stop();
    };
  }, [sim]);

  // Screen to drawing coordinates, whatever the SVG's scale or crop.
  const toLocal = (e: ReactPointerEvent) => {
    const m = svg.current!.getScreenCTM()!.inverse();
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m);
    return { x: p.x, y: p.y };
  };

  const neighbours = useMemo(() => {
    if (!hover) return null;
    const set = new Set([hover]);
    for (const e of graph.edges) {
      if (e.source === hover) set.add(e.target);
      if (e.target === hover) set.add(e.source);
    }
    return set;
  }, [hover, graph]);

  return (
    <svg
      ref={svg}
      className={styles.miniGraph}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid slice"
      role="img"
      aria-label={`${graph.nodes.length} nodes linked to CHK-131 in the preview knowledge graph. Drag a node to move it.`}
      onPointerMove={(e) => {
        if (!drag.current) return;
        const p = toLocal(e);
        drag.current.fx = p.x;
        drag.current.fy = p.y;
        sim.simulation.alphaTarget(0.2).restart();
      }}
      onPointerUp={() => {
        if (!drag.current) return;
        drag.current.fx = null;
        drag.current.fy = null;
        drag.current = null;
        sim.simulation.alphaTarget(0);
      }}
      onPointerLeave={() => setHover(null)}
    >
      {sim.links.map((l) => {
        const s = l.source as Node;
        const t = l.target as Node;
        const lit = neighbours && neighbours.has(s.id) && neighbours.has(t.id);
        return <line key={l.id} className={styles.miniEdge} data-lit={lit || undefined} data-dim={(neighbours && !lit) || undefined} x1={round(s.x)} y1={round(s.y)} x2={round(t.x)} y2={round(t.y)} />;
      })}
      {graph.nodes.map((n) => {
        const p = sim.byId.get(n.id)!;
        return (
          <g
            key={n.id}
            className={styles.miniNode}
            data-focus={n.focus}
            data-dim={(neighbours && !neighbours.has(n.id)) || undefined}
            transform={`translate(${round(p.x)},${round(p.y)})`}
            style={{ ["--c" as string]: TYPE_COLOR[n.type] }}
            onPointerEnter={() => setHover(n.id)}
            onPointerDown={(e) => {
              (e.target as Element).setPointerCapture?.(e.pointerId);
              drag.current = p;
            }}
          >
            <circle r={p.r + 5} className={styles.miniHalo} />
            <circle r={p.r} />
            <text y={p.r + 14}>{short(n.label)}</text>
          </g>
        );
      })}
    </svg>
  );
}
