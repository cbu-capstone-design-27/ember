"use client";

// The preview workspace in the browser. The server sends the snapshot (already
// cut down to the user's sources); this provider replays the user's edits on
// top of it and recomputes the model, insights and graph, so every page sees
// the same, current picture. Edits and dismissed insights are kept per user
// in localStorage: preview data is the user's own sandbox until real sources
// are connected, at which point each edit becomes a call to that source.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { applyEdits, type Edit, type EditBody, type Me } from "../../lib/workspace/edits.ts";
import { buildGraph, type GraphEdge, type GraphNode, type KnowledgeGraph } from "../../lib/workspace/graph.ts";
import { findInsights, type Insight, type Severity } from "../../lib/workspace/insights.ts";
import { buildModel, type Model } from "../../lib/workspace/model.ts";
import type { Workspace } from "../../lib/workspace/types.ts";
import { ItemDrawer } from "./item-drawer.tsx";
import { Toast } from "./toast.tsx";

interface Saved {
  edits: Edit[];
  dismissed: string[];
}

export interface NodeFlag {
  severity: Severity;
  insights: Insight[];
}

export type DrawerView = { kind: "item"; id: string } | { kind: "list"; title: string; ids: string[] };

export interface ToastState {
  id: number;
  text: string;
  undo?: () => void;
}

interface Preview {
  me: Me;
  now: number;
  workspace: Workspace;
  model: Model;
  /** Insights still open: not resolved by an edit and not dismissed. */
  insights: Insight[];
  dismissed: Insight[];
  graph: KnowledgeGraph;
  nodes: Map<string, GraphNode>;
  /** Each node's relationships, in both directions. */
  adjacency: Map<string, Array<{ edge: GraphEdge; other: string; out: boolean }>>;
  flags: Map<string, NodeFlag>;
  editCount: number;
  apply: (edit: EditBody, done: string) => void;
  dismiss: (insight: Insight) => void;
  restore: (insightId?: string) => void;
  reset: () => void;
  open: (view: DrawerView) => void;
  openItem: (id: string) => void;
  notify: (text: string) => void;
}

const PreviewContext = createContext<Preview | null>(null);

export function usePreview(): Preview {
  const value = useContext(PreviewContext);
  if (!value) throw new Error("usePreview needs <PreviewProvider>");
  return value;
}

// --- storage ----------------------------------------------------------------

const EMPTY: Saved = { edits: [], dismissed: [] };
const memory = new Map<string, string>(); // used when localStorage is blocked
const listeners = new Set<() => void>();
const parsed = new Map<string, { raw: string | null; value: Saved }>();

function readRaw(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return memory.get(key) ?? null;
  }
}

function read(key: string): Saved {
  const raw = readRaw(key);
  const hit = parsed.get(key);
  if (hit && hit.raw === raw) return hit.value;
  let value = EMPTY;
  try {
    const data = raw ? JSON.parse(raw) : null;
    if (data && Array.isArray(data.edits) && Array.isArray(data.dismissed)) value = data;
  } catch {
    // A damaged entry is treated as empty.
  }
  parsed.set(key, { raw, value });
  return value;
}

function write(key: string, value: Saved) {
  const raw = JSON.stringify(value);
  try {
    localStorage.setItem(key, raw);
  } catch {
    memory.set(key, raw);
  }
  for (const l of listeners) l();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = () => listener();
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

const RANK: Record<Severity, number> = { high: 0, medium: 1, low: 2 };

// --- provider ---------------------------------------------------------------

export function PreviewProvider({
  snapshot,
  serverNow,
  me,
  children,
}: {
  snapshot: Workspace;
  serverNow: number;
  me: Me;
  children: ReactNode;
}) {
  const key = `ember-preview:${me.id}`;
  const saved = useSyncExternalStore(subscribe, () => read(key), () => EMPTY);
  const [now, setNow] = useState(serverNow);
  const [stack, setStack] = useState<DrawerView[]>([]);
  const [toast, setToast] = useState<ToastState | null>(null);

  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

  const workspace = useMemo(() => applyEdits(snapshot, saved.edits, me), [snapshot, saved.edits, me]);
  const model = useMemo(() => buildModel(workspace), [workspace]);
  const all = useMemo(() => findInsights(model, now), [model, now]);
  const dismissedIds = useMemo(() => new Set(saved.dismissed), [saved.dismissed]);
  const insights = useMemo(() => all.filter((i) => !dismissedIds.has(i.id)), [all, dismissedIds]);
  const dismissed = useMemo(() => all.filter((i) => dismissedIds.has(i.id)), [all, dismissedIds]);
  // The graph is rebuilt when the data changes, not every time the clock ticks,
  // so the layout stays still; its "2h ago" fields are as of the last change.
  const clock = useRef(now);
  clock.current = now;
  const graph = useMemo(() => buildGraph(model, clock.current), [model]);
  const nodes = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
  const adjacency = useMemo(() => {
    const adj = new Map<string, Array<{ edge: GraphEdge; other: string; out: boolean }>>();
    const add = (id: string, entry: { edge: GraphEdge; other: string; out: boolean }) => adj.set(id, [...(adj.get(id) ?? []), entry]);
    for (const e of graph.edges) {
      add(e.source, { edge: e, other: e.target, out: true });
      add(e.target, { edge: e, other: e.source, out: false });
    }
    return adj;
  }, [graph]);
  const flags = useMemo(() => {
    const map = new Map<string, NodeFlag>();
    for (const i of insights) {
      const f = map.get(i.subject) ?? { severity: i.severity, insights: [] };
      if (RANK[i.severity] < RANK[f.severity]) f.severity = i.severity;
      f.insights.push(i);
      map.set(i.subject, f);
    }
    return map;
  }, [insights]);

  const notify = useCallback((text: string, undo?: () => void) => setToast({ id: Date.now(), text, undo }), []);

  const apply = useCallback(
    (body: EditBody, done: string) => {
      const at = new Date();
      const edit = { ...body, id: `e${at.getTime().toString(36)}${Math.random().toString(36).slice(2, 7)}`, at: at.toISOString() } as Edit;
      const current = read(key);
      write(key, { ...current, edits: [...current.edits, edit] });
      setNow(at.getTime());
      notify(done, () => {
        const latest = read(key);
        write(key, { ...latest, edits: latest.edits.filter((e) => e.id !== edit.id) });
      });
    },
    [key, notify],
  );

  const dismiss = useCallback(
    (insight: Insight) => {
      const current = read(key);
      write(key, { ...current, dismissed: [...new Set([...current.dismissed, insight.id])] });
      notify("Dismissed", () => {
        const latest = read(key);
        write(key, { ...latest, dismissed: latest.dismissed.filter((d) => d !== insight.id) });
      });
    },
    [key, notify],
  );

  const restore = useCallback(
    (insightId?: string) => {
      const current = read(key);
      write(key, { ...current, dismissed: insightId ? current.dismissed.filter((d) => d !== insightId) : [] });
    },
    [key],
  );

  const reset = useCallback(() => {
    write(key, EMPTY);
    setStack([]);
    notify("Preview data is back to the start");
  }, [key, notify]);

  const open = useCallback((view: DrawerView) => setStack((s) => [...s, view]), []);
  const openItem = useCallback((id: string) => open({ kind: "item", id }), [open]);

  const value = useMemo<Preview>(
    () => ({
      me,
      now,
      workspace,
      model,
      insights,
      dismissed,
      graph,
      nodes,
      adjacency,
      flags,
      editCount: saved.edits.length,
      apply,
      dismiss,
      restore,
      reset,
      open,
      openItem,
      notify: (text: string) => notify(text),
    }),
    [me, now, workspace, model, insights, dismissed, graph, nodes, adjacency, flags, saved.edits.length, apply, dismiss, restore, reset, open, openItem, notify],
  );

  return (
    <PreviewContext.Provider value={value}>
      {children}
      <ItemDrawer stack={stack} onBack={() => setStack((s) => s.slice(0, -1))} onClose={() => setStack([])} />
      <Toast toast={toast} onDone={() => setToast(null)} />
    </PreviewContext.Provider>
  );
}
