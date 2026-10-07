"use client";

// "Needs attention": the insight feed, filterable by kind. Each card shows
// the evidence from every source involved and links into the graph.

import Link from "next/link";
import { useMemo, useState } from "react";
import { SourceLogo } from "../../../components/brand.tsx";
import {
  BlockedIcon,
  CheckCircleIcon,
  ClockIcon,
  DecisionIcon,
  DriftIcon,
  GraphIcon,
  LinkIcon,
  SparkIcon,
  XCircleIcon,
} from "../../../components/icons.tsx";
import type { Insight, InsightKind } from "../../../lib/workspace/insights.ts";
import { ago, stamp } from "../../../lib/workspace/time.ts";
import styles from "./dashboard.module.css";

const KIND_ICON: Record<InsightKind, typeof BlockedIcon> = {
  blocker: BlockedIcon,
  failing_checks: XCircleIcon,
  status_drift: DriftIcon,
  stale_review: ClockIcon,
  missing_link: LinkIcon,
  decision: DecisionIcon,
};

const SEVERITY_LABEL = { high: "High", medium: "Medium", low: "Low" };

export function InsightsPanel({
  insights,
  kinds,
  now,
}: {
  insights: Insight[];
  kinds: Array<{ kind: InsightKind; label: string }>;
  now: number;
}) {
  const [filter, setFilter] = useState<InsightKind | "all">("all");
  const counts = useMemo(() => {
    const c = new Map<InsightKind, number>();
    for (const i of insights) c.set(i.kind, (c.get(i.kind) ?? 0) + 1);
    return c;
  }, [insights]);
  const shown = filter === "all" ? insights : insights.filter((i) => i.kind === filter);
  const present = kinds.filter((k) => counts.has(k.kind));

  return (
    <section className={`card ${styles.panel}`} aria-labelledby="attention-title">
      <div className="card-header">
        <div>
          <h2 id="attention-title">Needs attention</h2>
          <p>Where your tools disagree, or work is stuck. Newest evidence first within each level.</p>
        </div>
      </div>
      {insights.length === 0 ? (
        <div className={styles.allClear}>
          <CheckCircleIcon />
          <div>
            <h3>All clear</h3>
            <p className="muted">Nothing disagrees across your sources right now.</p>
          </div>
        </div>
      ) : (
        <>
          <div className={styles.filters} role="toolbar" aria-label="Filter insights">
            <button type="button" className="chip" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
              All <span className="count">{insights.length}</span>
            </button>
            {present.map(({ kind, label }) => (
              <button key={kind} type="button" className="chip" aria-pressed={filter === kind} onClick={() => setFilter(kind)}>
                {label} <span className="count">{counts.get(kind)}</span>
              </button>
            ))}
          </div>
          <ol className={styles.insights}>
            {shown.map((insight) => (
              <InsightCard key={insight.id} insight={insight} label={kinds.find((k) => k.kind === insight.kind)!.label} now={now} />
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function InsightCard({ insight, label, now }: { insight: Insight; label: string; now: number }) {
  const Icon = KIND_ICON[insight.kind];
  return (
    <li className={styles.insight} data-severity={insight.severity}>
      <span className={styles.insightIcon} data-kind={insight.kind} aria-hidden="true">
        <Icon />
      </span>
      <div className={styles.insightBody}>
        <div className={styles.insightMeta}>
          <span className={styles.insightKind}>{label}</span>
          <span className={styles.severity} data-severity={insight.severity}>
            {SEVERITY_LABEL[insight.severity]}
          </span>
          <time className={styles.insightTime} dateTime={insight.at} title={stamp(insight.at)}>
            {ago(insight.at, now)}
          </time>
        </div>
        <h3 className={styles.insightTitle}>{insight.title}</h3>
        <p className={styles.insightSummary}>{insight.summary}</p>

        <ul className={styles.evidence} aria-label="Evidence">
          {insight.evidence.map((e) => (
            <li key={e.nodeId}>
              <Link href={`/graph?focus=${encodeURIComponent(e.nodeId)}`} className={styles.evidenceRow} title="Show in the knowledge graph">
                <SourceLogo source={e.source} size={16} className={styles.evidenceLogo} />
                <span className={styles.evidenceLabel}>{e.label}</span>
                <span className={styles.evidenceDetail}>{e.detail}</span>
                <span className={e.quote ? styles.evidenceQuote : styles.evidenceText}>
                  {e.quote ? `“${e.text}”` : e.text}
                </span>
              </Link>
            </li>
          ))}
        </ul>

        <div className={styles.insightActions}>
          <span className={styles.suggestion}>
            <SparkIcon />
            <span>
              <span className="subtle">Suggested:</span> {insight.action}
            </span>
          </span>
          <Link className="btn btn-ghost btn-sm" href={`/graph?focus=${encodeURIComponent(insight.subject)}`}>
            <GraphIcon /> View in graph
          </Link>
        </div>
      </div>
    </li>
  );
}
