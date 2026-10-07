"use client";

// The knowledge graph explorer. d3-force computes the layout; React draws the
// SVG once per change of what's shown, and positions are written straight to
// the DOM on each simulation tick so dragging stays smooth.
//
// Interaction: drag a node to move it, drag the background to pan, scroll to
// zoom, click a node for its details and connections. "/" focuses search,
// Esc clears the selection, + / - zoom, 0 fits the graph to the screen.

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceX,
  forceY,
  forceSimulation,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { initials } from "../../../components/avatar.tsx";
import { SourceLogo } from "../../../components/brand.tsx";
import {
  ContainerIcon,
  DecisionIcon,
  FitIcon,
  FilterIcon,
  IdentityIcon,
  MessageIcon,
  MinusIcon,
  ModuleIcon,
  PlusIcon,
  PullRequestIcon,
  RefreshIcon,
  SearchIcon,
  ThreadIcon,
  TicketIcon,
  UserIcon,
  XIcon,
} from "../../../components/icons.tsx";
import type { SourceId } from "../../../lib/sources.ts";
import { NODE_TYPES, type EdgeType, type GraphEdge, type GraphNode, type KnowledgeGraph, type NodeType } from "../../../lib/workspace/graph.ts";
import type { Severity } from "../../../lib/workspace/insights.ts";
import styles from "./graph.module.css";

export interface NodeFlag {
  severity: Severity;
  insights: Array<{ title: string; action: string; severity: Severity }>;
}

type GroupKey = "github" | "jira" | "slack" | "people";

interface SimNode extends SimulationNodeDatum {
  id: string;
  type: NodeType;
  group: GroupKey;
  r: number;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  id: string;
  type: EdgeType;
}

interface View {
  x: number;
  y: number;
  k: number;
}

const TYPE_ORDER: NodeType[] = ["WorkItem", "Change", "Message", "Person", "Decision", "Conversation", "Container", "Module", "Identity"];

const TYPE_COLOR: Record<NodeType, string> = {
  WorkItem: "var(--node-workitem)",
  Change: "var(--node-change)",
  Message: "var(--node-message)",
  Person: "var(--node-person)",
  Decision: "var(--node-decision)",
  Conversation: "var(--node-conversation)",
  Container: "var(--node-container)",
  Module: "var(--node-module)",
  Identity: "var(--node-identity)",
};

const TYPE_ICON: Record<NodeType, typeof UserIcon> = {
  WorkItem: TicketIcon,
  Change: PullRequestIcon,
  Message: MessageIcon,
  Person: UserIcon,
  Decision: DecisionIcon,
  Conversation: ThreadIcon,
  Container: ContainerIcon,
  Module: ModuleIcon,
  Identity: IdentityIcon,
};

const RADIUS: Record<NodeType, number> = {
  Person: 16,
  Container: 14,
  WorkItem: 11,
  Change: 10.5,
  Decision: 12,
  Module: 9,
  Conversation: 8,
  Message: 6.5,
  Identity: 6,
};

/** Types whose labels show at any zoom; the rest appear when zoomed in or highlighted. */
const MAJOR = new Set<NodeType>(["Person", "Container", "WorkItem", "Change", "Decision", "Module"]);

/** Where each source's subgraph sits when the layout is grouped. People sit between them. */
const GROUP_CENTER: Record<GroupKey, { x: number; y: number }> = {
  jira: { x: 0, y: -320 },
  github: { x: -420, y: 210 },
  slack: { x: 420, y: 210 },
  people: { x: 0, y: 40 },
};

const LINK_DISTANCE: Partial<Record<EdgeType, number>> = {
  RESOLVES_TO: 26,
  PART_OF: 34,
  REPLIES_TO: 30,
  CONTAINS: 90,
  TOUCHES: 50,
  RELATES_TO: 70,
  REFERENCES: 75,
  AUTHORED: 75,
  ASSIGNED_TO: 75,
};

const SOURCE_LABEL: Record<string, string> = { github: "GitHub", jira: "Jira", slack: "Slack", gitlab: "GitLab", teams: "Teams" };

function groupOf(n: GraphNode): GroupKey {
  if (n.type === "Person") return "people";
  const source = n.source ?? n.subgraph?.split("_").pop();
  return source === "github" || source === "jira" || source === "slack" ? source : "people";
}

/** Source filter key: Person nodes are cross-source and always shown. */
function sourceKey(n: GraphNode): SourceId | null {
  if (n.type === "Person") return null;
  const s = n.source ?? n.subgraph?.split("_").pop();
  return (s as SourceId) ?? null;
}

function hash(text: string): number {
  let h = 2166136261;
  for (const ch of text) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return (h >>> 0) / 4294967296;
}

function useMounted() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}

export function GraphExplorer(props: {
  graph: KnowledgeGraph;
  flags: Record<string, NodeFlag>;
  focus?: string;
  tenant: string;
  organization: string;
}) {
  // The layout needs real screen size and runs only in the browser.
  const mounted = useMounted();
  return (
    <div className={styles.page}>
      {mounted ? (
        <Explorer {...props} />
      ) : (
        <div className={styles.loading}>
          <span className="spinner" aria-hidden="true" /> Laying out the graph…
        </div>
      )}
    </div>
  );
}

function Explorer({
  graph,
  flags,
  focus,
  tenant,
  organization,
}: {
  graph: KnowledgeGraph;
  flags: Record<string, NodeFlag>;
  focus?: string;
  tenant: string;
  organization: string;
}) {
  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
  const adjacency = useMemo(() => {
    const adj = new Map<string, Array<{ edge: GraphEdge; other: string; out: boolean }>>();
    for (const e of graph.edges) {
      (adj.get(e.source) ?? adj.set(e.source, []).get(e.source)!).push({ edge: e, other: e.target, out: true });
      (adj.get(e.target) ?? adj.set(e.target, []).get(e.target)!).push({ edge: e, other: e.source, out: false });
    }
    return adj;
  }, [graph]);

  const availableSources = useMemo(
    () => [...new Set(graph.nodes.map(sourceKey).filter((s): s is SourceId => !!s))],
    [graph],
  );
  const typeCounts = useMemo(() => {
    const c = new Map<NodeType, number>();
    for (const n of graph.nodes) c.set(n.type, (c.get(n.type) ?? 0) + 1);
    return c;
  }, [graph]);

  const initialFocus = focus && byId.has(focus) ? focus : null;
  const [types, setTypes] = useState<Set<NodeType>>(() => {
    const all = new Set(TYPE_ORDER.filter((t) => typeCounts.has(t)));
    // Identities clutter the picture; show them only when asked, or when one is the focus.
    if (!(initialFocus && byId.get(initialFocus)!.type === "Identity")) all.delete("Identity");
    return all;
  });
  const [sources, setSources] = useState<Set<SourceId>>(() => new Set(availableSources));
  const [grouped, setGrouped] = useState(true);
  const [selected, setSelected] = useState<string | null>(initialFocus);
  const [hovered, setHovered] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(() => window.innerWidth > 760);

  const isVisible = useCallback(
    (n: GraphNode) => types.has(n.type) && (sourceKey(n) === null || sources.has(sourceKey(n)!)),
    [types, sources],
  );
  const visibleNodes = useMemo(() => graph.nodes.filter(isVisible), [graph, isVisible]);
  const visibleIds = useMemo(() => new Set(visibleNodes.map((n) => n.id)), [visibleNodes]);
  const visibleEdges = useMemo(
    () => graph.edges.filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target)),
    [graph, visibleIds],
  );
  const degree = useMemo(() => {
    const d = new Map<string, number>();
    for (const e of visibleEdges) {
      d.set(e.source, (d.get(e.source) ?? 0) + 1);
      d.set(e.target, (d.get(e.target) ?? 0) + 1);
    }
    return d;
  }, [visibleEdges]);

  // --- simulation --------------------------------------------------------

  const positions = useRef(new Map<string, SimNode>());
  const svgRef = useRef<SVGSVGElement>(null);
  const viewportRef = useRef<SVGGElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const nodeEls = useRef(new Map<string, SVGGElement>());
  const edgeEls = useRef(new Map<string, SVGLineElement>());
  const labelEls = useRef(new Map<string, SVGTextElement>());
  const view = useRef<View>({ x: 0, y: 0, k: 1 });
  const frame = useRef(0);
  const firstLayout = useRef(true);

  const sim = useMemo(() => {
    const nodes: SimNode[] = visibleNodes.map((n) => {
      const known = positions.current.get(n.id);
      const r = RADIUS[n.type] + (n.type === "WorkItem" && n.state === "epic" ? 4 : 0) + Math.min(4, Math.sqrt(degree.get(n.id) ?? 0) * 0.6);
      if (known) {
        known.r = r;
        return known;
      }
      // New nodes start near a placed neighbor, or in their group's area.
      const neighbor = (adjacency.get(n.id) ?? []).map((a) => positions.current.get(a.other)).find(Boolean);
      const base = neighbor ?? GROUP_CENTER[groupOf(n)];
      const angle = hash(n.id) * Math.PI * 2;
      const created: SimNode = {
        id: n.id,
        type: n.type,
        group: groupOf(n),
        r,
        x: (base.x ?? 0) + Math.cos(angle) * 40,
        y: (base.y ?? 0) + Math.sin(angle) * 40,
      };
      positions.current.set(n.id, created);
      return created;
    });
    const links: SimLink[] = visibleEdges.map((e) => ({ id: e.id, type: e.type, source: e.source, target: e.target }));

    const simulation: Simulation<SimNode, SimLink> = forceSimulation(nodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance((l) => LINK_DISTANCE[l.type] ?? 65)
          .strength((l) => (l.type === "CONTAINS" ? 0.12 : l.type === "RESOLVES_TO" ? 0.9 : 0.45)),
      )
      .force("charge", forceManyBody<SimNode>().strength((d) => (MAJOR.has(d.type) ? -320 : -90)).distanceMax(520))
      // Major nodes keep room for their label underneath.
      .force("collide", forceCollide<SimNode>((d) => d.r + (MAJOR.has(d.type) ? 16 : 5)).iterations(2))
      .force("x", forceX<SimNode>((d) => (grouped ? GROUP_CENTER[d.group].x : 0)).strength(grouped ? 0.09 : 0.03))
      .force("y", forceY<SimNode>((d) => (grouped ? GROUP_CENTER[d.group].y : 0)).strength(grouped ? 0.09 : 0.03))
      .stop();

    if (firstLayout.current) {
      // Settle most of the way before the first paint.
      simulation.alpha(1);
      for (let i = 0; i < 260; i++) simulation.tick();
      simulation.alpha(0.12);
    } else {
      simulation.alpha(0.6);
    }
    return { simulation, nodes, links, byId: new Map(nodes.map((n) => [n.id, n])) };
    // degree and adjacency follow visibleNodes/visibleEdges; grouped changes the forces.
  }, [visibleNodes, visibleEdges, grouped]);

  const writePositions = useCallback(() => {
    for (const n of sim.nodes) {
      nodeEls.current.get(n.id)?.setAttribute("transform", `translate(${n.x!.toFixed(1)},${n.y!.toFixed(1)})`);
    }
    for (const l of sim.links) {
      const s = l.source as SimNode;
      const t = l.target as SimNode;
      const line = edgeEls.current.get(l.id);
      if (line) {
        const dx = t.x! - s.x!;
        const dy = t.y! - s.y!;
        const len = Math.hypot(dx, dy) || 1;
        const cut = Math.min(t.r + 3, len / 2);
        line.setAttribute("x1", s.x!.toFixed(1));
        line.setAttribute("y1", s.y!.toFixed(1));
        line.setAttribute("x2", (t.x! - (dx / len) * cut).toFixed(1));
        line.setAttribute("y2", (t.y! - (dy / len) * cut).toFixed(1));
      }
      const label = labelEls.current.get(l.id);
      if (label) {
        label.setAttribute("x", ((s.x! + t.x!) / 2).toFixed(1));
        label.setAttribute("y", ((s.y! + t.y!) / 2).toFixed(1));
      }
    }
  }, [sim]);

  const run = useCallback(() => {
    if (frame.current) return;
    const step = () => {
      sim.simulation.tick();
      writePositions();
      if (sim.simulation.alpha() > sim.simulation.alphaMin()) frame.current = requestAnimationFrame(step);
      else frame.current = 0;
    };
    frame.current = requestAnimationFrame(step);
  }, [sim, writePositions]);

  useEffect(() => {
    writePositions();
    run();
    return () => {
      cancelAnimationFrame(frame.current);
      frame.current = 0;
    };
  }, [sim, run, writePositions]);

  // --- view (pan and zoom) ----------------------------------------------

  const applyView = useCallback(() => {
    const { x, y, k } = view.current;
    viewportRef.current?.setAttribute("transform", `translate(${x},${y}) scale(${k})`);
    const canvas = canvasRef.current;
    if (canvas) {
      canvas.style.backgroundPosition = `${x}px ${y}px`;
      canvas.style.backgroundSize = `${22 * k}px ${22 * k}px`;
      canvas.dataset.zoom = k < 0.55 ? "far" : k > 1.35 ? "near" : "mid";
    }
  }, []);

  const animateTo = useCallback(
    (target: View, ms = 380) => {
      const from = { ...view.current };
      const start = performance.now();
      const ease = (t: number) => 1 - Math.pow(1 - t, 3);
      const step = (now: number) => {
        const t = Math.min(1, (now - start) / ms);
        const e = ease(t);
        view.current = { x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e };
        applyView();
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    [applyView],
  );

  // The part of the canvas not covered by search, filters or the detail panel.
  const panelOpen = selected !== null;
  const usableArea = useCallback(() => {
    const rect = svgRef.current!.getBoundingClientRect();
    const narrow = rect.width < 760;
    const left = filtersOpen && !narrow ? 296 : 0;
    const right = panelOpen && !narrow ? 392 : 0;
    const top = 64;
    const bottom = panelOpen && narrow ? rect.height * 0.5 : 24;
    return { x: left, y: top, width: rect.width - left - right, height: rect.height - top - bottom, narrow };
  }, [panelOpen, filtersOpen]);

  const fit = useCallback(
    (animate = true) => {
      if (!svgRef.current || sim.nodes.length === 0) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of sim.nodes) {
        minX = Math.min(minX, n.x! - n.r);
        minY = Math.min(minY, n.y! - n.r);
        maxX = Math.max(maxX, n.x! + n.r);
        maxY = Math.max(maxY, n.y! + n.r);
      }
      const area = usableArea();
      const pad = area.narrow ? 16 : 40;
      const k = Math.max(0.15, Math.min(1.6, Math.min((area.width - pad * 2) / (maxX - minX), (area.height - pad * 2) / (maxY - minY))));
      const target = {
        k,
        x: area.x + area.width / 2 - ((minX + maxX) / 2) * k,
        y: area.y + area.height / 2 - ((minY + maxY) / 2) * k,
      };
      if (animate) animateTo(target);
      else {
        view.current = target;
        applyView();
      }
    },
    [sim, usableArea, animateTo, applyView],
  );

  const centerOn = useCallback(
    (id: string) => {
      const n = sim.byId.get(id);
      if (!n || !svgRef.current) return;
      const area = usableArea();
      const k = Math.max(view.current.k, 1.15);
      animateTo({ k, x: area.x + area.width / 2 - n.x! * k, y: area.y + area.height / 2 - n.y! * k });
    },
    [sim, usableArea, animateTo],
  );

  const zoomBy = useCallback(
    (factor: number, cx?: number, cy?: number) => {
      const rect = svgRef.current!.getBoundingClientRect();
      const px = cx ?? rect.width / 2;
      const py = cy ?? rect.height / 2;
      const { x, y, k } = view.current;
      const nk = Math.max(0.15, Math.min(4, k * factor));
      view.current = { k: nk, x: px - ((px - x) / k) * nk, y: py - ((py - y) / k) * nk };
      applyView();
    },
    [applyView],
  );

  // First paint: fit everything, then go to the focused node if there is one.
  useEffect(() => {
    if (!firstLayout.current) return;
    firstLayout.current = false;
    fit(false);
    if (initialFocus) centerOn(initialFocus);
    // Only on mount.
  }, []);

  useEffect(() => {
    const svg = svgRef.current!;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      zoomBy(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018)), e.clientX - rect.left, e.clientY - rect.top);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [zoomBy]);

  // --- pointer: pan, drag, click ------------------------------------------

  const gesture = useRef<
    | { kind: "pan"; startX: number; startY: number; from: View; moved: boolean }
    | { kind: "drag"; node: SimNode; startX: number; startY: number; moved: boolean }
    | null
  >(null);

  const toGraph = (clientX: number, clientY: number) => {
    const rect = svgRef.current!.getBoundingClientRect();
    const { x, y, k } = view.current;
    return { x: (clientX - rect.left - x) / k, y: (clientY - rect.top - y) / k };
  };

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    const target = (e.target as Element).closest("[data-node]");
    svgRef.current!.setPointerCapture(e.pointerId);
    if (target) {
      const node = sim.byId.get(target.getAttribute("data-node")!);
      if (!node) return;
      gesture.current = { kind: "drag", node, startX: e.clientX, startY: e.clientY, moved: false };
    } else {
      gesture.current = { kind: "pan", startX: e.clientX, startY: e.clientY, from: { ...view.current }, moved: false };
    }
  };

  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const g = gesture.current;
    if (!g) return;
    const dist = Math.hypot(e.clientX - g.startX, e.clientY - g.startY);
    if (!g.moved && dist < 4) return;
    g.moved = true;
    if (g.kind === "pan") {
      view.current = { ...g.from, x: g.from.x + e.clientX - g.startX, y: g.from.y + e.clientY - g.startY };
      applyView();
    } else {
      const p = toGraph(e.clientX, e.clientY);
      g.node.fx = p.x;
      g.node.fy = p.y;
      sim.simulation.alphaTarget(0.25);
      if (sim.simulation.alpha() < 0.25) sim.simulation.alpha(0.25);
      run();
    }
  };

  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    const g = gesture.current;
    gesture.current = null;
    if (svgRef.current!.hasPointerCapture(e.pointerId)) svgRef.current!.releasePointerCapture(e.pointerId);
    if (!g) return;
    if (g.kind === "drag") {
      g.node.fx = null;
      g.node.fy = null;
      sim.simulation.alphaTarget(0);
      if (!g.moved) select(g.node.id, false);
    } else if (!g.moved) {
      select(null);
    }
  };

  // --- selection, search, keyboard -------------------------------------------

  const select = useCallback(
    (id: string | null, pan = true) => {
      setSelected(id);
      const url = new URL(window.location.href);
      if (id) url.searchParams.set("focus", id);
      else url.searchParams.delete("focus");
      window.history.replaceState(window.history.state, "", url);
      if (id && pan) requestAnimationFrame(() => centerOn(id));
    },
    [centerOn],
  );

  /** Select a node even if filters hide it: turn its type and source back on first. */
  const reveal = useCallback(
    (id: string) => {
      const n = byId.get(id);
      if (!n) return;
      if (!types.has(n.type)) setTypes((t) => new Set(t).add(n.type));
      const s = sourceKey(n);
      if (s && !sources.has(s)) setSources((v) => new Set(v).add(s));
      // Wait a frame so a newly shown node has a position to pan to.
      requestAnimationFrame(() => select(id));
    },
    [byId, types, sources, select],
  );

  const searchRef = useRef<HTMLInputElement>(null);
  const [searchIndex, setSearchIndex] = useState(0);
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return graph.nodes
      .filter((n) => n.label.toLowerCase().includes(q) || n.title.toLowerCase().includes(q) || (n.body ?? "").toLowerCase().includes(q))
      .sort((a, b) => TYPE_ORDER.indexOf(a.type) - TYPE_ORDER.indexOf(b.type))
      .slice(0, 8);
  }, [graph, query]);
  const matches = useMemo(() => new Set(results.map((r) => r.id)), [results]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement;
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape") {
        if (typing) (e.target as HTMLElement).blur();
        setQuery("");
        select(null);
      } else if (!typing && (e.key === "+" || e.key === "=")) zoomBy(1.25);
      else if (!typing && e.key === "-") zoomBy(0.8);
      else if (!typing && e.key === "0") fit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [select, zoomBy, fit]);

  // --- highlight ------------------------------------------------------------

  const active = hovered ?? selected;
  const neighborhood = useMemo(() => {
    if (!active) return null;
    const set = new Set([active]);
    for (const a of adjacency.get(active) ?? []) set.add(a.other);
    return set;
  }, [active, adjacency]);

  const nodeClass = (n: GraphNode) => {
    const cls = [styles.node];
    if (!MAJOR.has(n.type)) cls.push(styles.minor);
    if (n.id === selected) cls.push(styles.selected);
    if (neighborhood) cls.push(neighborhood.has(n.id) ? styles.lit : styles.dim);
    else if (matches.size) cls.push(matches.has(n.id) ? styles.lit : styles.dim);
    return cls.join(" ");
  };

  const edgeLit = (e: GraphEdge) => !!active && (e.source === active || e.target === active);

  // --- render -----------------------------------------------------------------

  const selectedNode = selected ? byId.get(selected) : undefined;
  const hiddenCount = graph.nodes.length - visibleNodes.length;

  return (
    <>
      <div ref={canvasRef} className={styles.canvas} data-zoom="mid">
        <svg
          ref={svgRef}
          className={styles.svg}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          role="img"
          aria-label={`Knowledge graph of ${organization}: ${visibleNodes.length} nodes and ${visibleEdges.length} relationships. Use the search box to find and select a node.`}
        >
          <defs>
            <marker id="graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 1 9 5 0 9z" fill="var(--graph-edge-strong)" />
            </marker>
          </defs>
          <g ref={viewportRef}>
            <g className={styles.edges}>
              {visibleEdges.map((e) => (
                <line
                  key={e.id}
                  ref={(el) => {
                    if (el) edgeEls.current.set(e.id, el);
                    else edgeEls.current.delete(e.id);
                  }}
                  className={edgeLit(e) ? styles.edgeLit : active ? styles.edgeDim : styles.edge}
                  markerEnd={edgeLit(e) ? "url(#graph-arrow)" : undefined}
                />
              ))}
            </g>
            <g className={styles.edgeLabels}>
              {visibleEdges.filter(edgeLit).map((e) => (
                <text
                  key={e.id}
                  ref={(el) => {
                    if (el) labelEls.current.set(e.id, el);
                    else labelEls.current.delete(e.id);
                  }}
                  className={styles.edgeLabel}
                  x={(((sim.byId.get(e.source)?.x ?? 0) + (sim.byId.get(e.target)?.x ?? 0)) / 2).toFixed(1)}
                  y={(((sim.byId.get(e.source)?.y ?? 0) + (sim.byId.get(e.target)?.y ?? 0)) / 2).toFixed(1)}
                >
                  {e.type}
                </text>
              ))}
            </g>
            <g>
              {visibleNodes.map((n) => {
                const s = sim.byId.get(n.id)!;
                const flag = flags[n.id];
                const Icon = TYPE_ICON[n.type];
                return (
                  <g
                    key={n.id}
                    data-node={n.id}
                    ref={(el) => {
                      if (el) nodeEls.current.set(n.id, el);
                      else nodeEls.current.delete(n.id);
                    }}
                    transform={`translate(${s.x!.toFixed(1)},${s.y!.toFixed(1)})`}
                    className={nodeClass(n)}
                    style={{ ["--c" as string]: TYPE_COLOR[n.type] }}
                    onPointerEnter={() => setHovered(n.id)}
                    onPointerLeave={() => setHovered((h) => (h === n.id ? null : h))}
                  >
                    <circle className={styles.halo} r={s.r + 6} />
                    <circle className={styles.dot} r={s.r} data-state={n.state} />
                    {n.type === "Person" ? (
                      <text className={styles.initials} dy="0.35em">
                        {initials(n.label)}
                      </text>
                    ) : s.r >= 9 ? (
                      <Icon className={styles.glyph} x={-s.r * 0.55} y={-s.r * 0.55} width={s.r * 1.1} height={s.r * 1.1} />
                    ) : null}
                    {flag && <circle className={styles.flag} data-severity={flag.severity} cx={s.r * 0.75} cy={-s.r * 0.75} r={4.5} />}
                    <text className={styles.label} y={s.r + 13}>
                      {n.label}
                    </text>
                  </g>
                );
              })}
            </g>
          </g>
        </svg>
      </div>

      {/* Search */}
      <div className={styles.searchBox}>
        <SearchIcon className={`icon ${styles.searchIcon}`} />
        <input
          ref={searchRef}
          className={styles.searchInput}
          placeholder="Search people, tickets, PRs, messages…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setSearchIndex(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSearchIndex((i) => Math.min(results.length - 1, i + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setSearchIndex((i) => Math.max(0, i - 1));
            } else if (e.key === "Enter" && results[searchIndex]) {
              reveal(results[searchIndex].id);
              setQuery("");
              e.currentTarget.blur();
            }
          }}
          role="combobox"
          aria-expanded={results.length > 0}
          aria-controls="graph-search-results"
          aria-activedescendant={results[searchIndex] ? `result-${searchIndex}` : undefined}
          aria-label="Search the graph"
        />
        {query ? (
          <button type="button" className="btn btn-ghost btn-icon btn-sm" aria-label="Clear search" onClick={() => setQuery("")}>
            <XIcon />
          </button>
        ) : (
          <span className="kbd" aria-hidden="true">
            /
          </span>
        )}
        {results.length > 0 && (
          <ul id="graph-search-results" className={styles.results} role="listbox">
            {results.map((r, i) => (
              <li
                key={r.id}
                id={`result-${i}`}
                role="option"
                aria-selected={i === searchIndex}
                className={styles.result}
                onPointerDown={(e) => {
                  e.preventDefault();
                  reveal(r.id);
                  setQuery("");
                }}
                onPointerEnter={() => setSearchIndex(i)}
              >
                <span className={styles.typeDot} style={{ background: TYPE_COLOR[r.type] }} />
                <span className={styles.resultText}>
                  <span className={styles.resultLabel}>{r.title}</span>
                  <span className={styles.resultMeta}>
                    {NODE_TYPES[r.type].label} · {r.subtitle}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
        {query && results.length === 0 && <div className={styles.results}><p className={styles.noResults}>No matches</p></div>}
      </div>

      {/* Filters */}
      <aside className={`${styles.filters} ${filtersOpen ? "" : styles.filtersClosed}`} aria-label="Graph filters">
        <button type="button" className={styles.filtersToggle} onClick={() => setFiltersOpen((o) => !o)} aria-expanded={filtersOpen}>
          <FilterIcon /> <span>Filters</span>
          {hiddenCount > 0 && <span className="badge">{hiddenCount} hidden</span>}
        </button>
        {filtersOpen && (
          <div className={styles.filtersBody}>
            <fieldset className={styles.fieldset}>
              <legend>Subgraphs</legend>
              {availableSources.map((s) => (
                <label key={s} className={styles.check}>
                  <input
                    type="checkbox"
                    checked={sources.has(s)}
                    onChange={() =>
                      setSources((v) => {
                        const next = new Set(v);
                        if (next.has(s)) next.delete(s);
                        else next.add(s);
                        return next;
                      })
                    }
                  />
                  <SourceLogo source={s} size={14} />
                  <span>{SOURCE_LABEL[s]}</span>
                  <code className={styles.subgraph}>
                    {tenant}_{s}
                  </code>
                </label>
              ))}
            </fieldset>
            <fieldset className={styles.fieldset}>
              <legend>Node types</legend>
              {TYPE_ORDER.filter((t) => typeCounts.has(t)).map((t) => (
                <label key={t} className={styles.check} title={NODE_TYPES[t].description}>
                  <input
                    type="checkbox"
                    checked={types.has(t)}
                    onChange={() =>
                      setTypes((v) => {
                        const next = new Set(v);
                        if (next.has(t)) next.delete(t);
                        else next.add(t);
                        return next;
                      })
                    }
                  />
                  <span className={styles.typeDot} style={{ background: TYPE_COLOR[t] }} />
                  <span>{NODE_TYPES[t].plural}</span>
                  <span className={styles.count}>{typeCounts.get(t)}</span>
                </label>
              ))}
            </fieldset>
            <label className={styles.switchRow}>
              <input type="checkbox" role="switch" checked={grouped} onChange={() => setGrouped((g) => !g)} />
              <span>Group by source</span>
            </label>
          </div>
        )}
      </aside>

      {/* Zoom */}
      <div className={styles.zoom} role="group" aria-label="Zoom">
        <button type="button" className="btn btn-secondary btn-icon btn-sm" onClick={() => zoomBy(1.25)} aria-label="Zoom in" title="Zoom in (+)">
          <PlusIcon />
        </button>
        <button type="button" className="btn btn-secondary btn-icon btn-sm" onClick={() => zoomBy(0.8)} aria-label="Zoom out" title="Zoom out (-)">
          <MinusIcon />
        </button>
        <button type="button" className="btn btn-secondary btn-icon btn-sm" onClick={() => fit()} aria-label="Fit to screen" title="Fit to screen (0)">
          <FitIcon />
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-icon btn-sm"
          aria-label="Re-run layout"
          title="Re-run layout"
          onClick={() => {
            for (const n of sim.nodes) {
              const a = hash(n.id) * Math.PI * 2;
              n.x = GROUP_CENTER[n.group].x + Math.cos(a) * 120;
              n.y = GROUP_CENTER[n.group].y + Math.sin(a) * 120;
            }
            sim.simulation.alpha(1);
            run();
            setTimeout(() => fit(), 900);
          }}
        >
          <RefreshIcon />
        </button>
      </div>

      <p className={styles.status}>
        {visibleNodes.length} nodes · {visibleEdges.length} relationships · Preview data
      </p>

      {selectedNode && (
        <DetailPanel
          node={selectedNode}
          flag={flags[selectedNode.id]}
          connections={adjacency.get(selectedNode.id) ?? []}
          byId={byId}
          isVisible={(id) => visibleIds.has(id)}
          onSelect={reveal}
          onClose={() => select(null)}
        />
      )}
    </>
  );
}

function DetailPanel({
  node,
  flag,
  connections,
  byId,
  isVisible,
  onSelect,
  onClose,
}: {
  node: GraphNode;
  flag?: NodeFlag;
  connections: Array<{ edge: GraphEdge; other: string; out: boolean }>;
  byId: Map<string, GraphNode>;
  isVisible: (id: string) => boolean;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const groups = useMemo(() => {
    const map = new Map<string, Array<{ edge: GraphEdge; other: GraphNode }>>();
    for (const c of connections) {
      const other = byId.get(c.other);
      if (!other) continue;
      const key = c.out ? `${c.edge.type} →` : `← ${c.edge.type}`;
      (map.get(key) ?? map.set(key, []).get(key)!).push({ edge: c.edge, other });
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [connections, byId]);
  const Icon = TYPE_ICON[node.type];

  return (
    <aside className={styles.panel} aria-label={`Details for ${node.title}`}>
      <div className={styles.panelHead}>
        <span className={styles.panelType} style={{ ["--c" as string]: TYPE_COLOR[node.type] }}>
          <Icon /> {NODE_TYPES[node.type].label}
        </span>
        {node.source && <SourceLogo source={node.source} size={16} />}
        <button type="button" className="btn btn-ghost btn-icon btn-sm" onClick={onClose} aria-label="Close details" style={{ marginLeft: "auto" }}>
          <XIcon />
        </button>
      </div>
      <div className={styles.panelBody}>
        <h2 className={styles.panelTitle}>{node.title}</h2>
        <p className={styles.panelSubtitle}>{node.subtitle}</p>

        {flag && (
          <div className={styles.panelInsights}>
            {flag.insights.map((i) => (
              <Link key={i.title} href="/dashboard" className={styles.panelInsight} data-severity={i.severity}>
                <span className={styles.panelInsightTitle}>{i.title}</span>
                <span className="subtle">Suggested: {i.action}</span>
              </Link>
            ))}
          </div>
        )}

        {node.body && <p className={styles.panelText}>{node.body}</p>}

        {node.fields.length > 0 && (
          <dl className={styles.fields}>
            {node.fields.map((f) => (
              <div key={f.label}>
                <dt>{f.label}</dt>
                <dd>{f.value}</dd>
              </div>
            ))}
            {node.subgraph && (
              <div>
                <dt>Subgraph</dt>
                <dd className="mono">{node.subgraph}</dd>
              </div>
            )}
          </dl>
        )}

        <h3 className={styles.connectionsTitle}>
          Connections <span className="subtle">{connections.length}</span>
        </h3>
        {groups.map(([key, items]) => (
          <div key={key} className={styles.connGroup}>
            <p className={styles.connType}>{key}</p>
            <ul>
              {items.map(({ edge, other }) => (
                <li key={edge.id}>
                  <button type="button" className={styles.conn} onClick={() => onSelect(other.id)} data-hidden={!isVisible(other.id)}>
                    <span className={styles.typeDot} style={{ background: TYPE_COLOR[other.type] }} />
                    <span className={styles.connLabel}>{other.type === "Message" ? other.title : other.label}</span>
                    {edge.detail && <span className={styles.connDetail}>{edge.detail}</span>}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
        <p className={styles.panelFoot}>Preview data: these items don&apos;t link out to a real {node.source ? SOURCE_LABEL[node.source] : "source"}.</p>
      </div>
    </aside>
  );
}
