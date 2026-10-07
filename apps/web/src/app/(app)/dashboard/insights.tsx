"use client";

// "Needs attention": the insight feed, filterable by kind. Each card opens
// the item it's about; its evidence opens the item each source has; and its
// action makes the change, after which the insight goes away by itself.

import Link from "next/link";
import { useMemo, useState, type MouseEvent } from "react";
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
  XIcon,
} from "../../../components/icons.tsx";
import { usePreview } from "../../../components/preview/store.tsx";
import { INSIGHT_KINDS, type Insight, type InsightKind } from "../../../lib/workspace/insights.ts";
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

const KINDS = (Object.entries(INSIGHT_KINDS) as Array<[InsightKind, { label: string; order: number }]>)
  .sort((a, b) => a[1].order - b[1].order)
  .map(([kind, { label }]) => ({ kind, label }));

export function InsightsPanel() {
  const { insights, dismissed, restore, now } = usePreview();
  const [filter, setFilter] = useState<InsightKind | "all">("all");
  const [showDismissed, setShowDismissed] = useState(false);
  const counts = useMemo(() => {
    const c = new Map<InsightKind, number>();
    for (const i of insights) c.set(i.kind, (c.get(i.kind) ?? 0) + 1);
    return c;
  }, [insights]);
  const active = filter !== "all" && !counts.has(filter) ? "all" : filter;
  const shown = active === "all" ? insights : insights.filter((i) => i.kind === active);

  return (
    <section id="attention" className={`card ${styles.panel}`} aria-labelledby="attention-title">
      <div className="card-header">
        <div>
          <h2 id="attention-title">Needs attention</h2>
          <p>Where your tools disagree, or work is stuck. Click one to see everything behind it.</p>
        </div>
        {dismissed.length > 0 && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowDismissed((s) => !s)} aria-expanded={showDismissed}>
            {showDismissed ? "Hide" : "Show"} {dismissed.length} dismissed
          </button>
        )}
      </div>

      {showDismissed && dismissed.length > 0 && (
        <ul className={styles.dismissedList}>
          {dismissed.map((i) => (
            <li key={i.id}>
              <span>{i.title}</span>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => restore(i.id)}>
                Restore
              </button>
            </li>
          ))}
        </ul>
      )}

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
            <button type="button" className="chip" aria-pressed={active === "all"} onClick={() => setFilter("all")}>
              All <span className="count">{insights.length}</span>
            </button>
            {KINDS.filter((k) => counts.has(k.kind)).map(({ kind, label }) => (
              <button key={kind} type="button" className="chip" aria-pressed={active === kind} onClick={() => setFilter(kind)}>
                {label} <span className="count">{counts.get(kind)}</span>
              </button>
            ))}
          </div>
          <ol className={styles.insights}>
            {shown.map((insight) => (
              <InsightCard key={insight.id} insight={insight} now={now} />
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function InsightCard({ insight, now }: { insight: Insight; now: number }) {
  const { apply, dismiss, openItem } = usePreview();
  const Icon = KIND_ICON[insight.kind];
  const label = INSIGHT_KINDS[insight.kind].label;

  // The whole card opens the item, except where a button or link inside it was clicked.
  const onCardClick = (e: MouseEvent) => {
    if ((e.target as Element).closest("button, a")) return;
    openItem(insight.subject);
  };

  return (
    <li className={styles.insight} data-severity={insight.severity} onClick={onCardClick}>
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
          <button type="button" className={`btn btn-ghost btn-icon btn-sm ${styles.dismiss}`} onClick={() => dismiss(insight)} aria-label={`Dismiss: ${insight.title}`} title="Dismiss">
            <XIcon />
          </button>
        </div>
        <h3 className={styles.insightTitle}>
          <button type="button" className={styles.insightTitleButton} onClick={() => openItem(insight.subject)}>
            {insight.title}
          </button>
        </h3>
        <p className={styles.insightSummary}>{insight.summary}</p>

        <ul className={styles.evidence} aria-label="Evidence">
          {insight.evidence.map((e) => (
            <li key={e.nodeId}>
              <button type="button" className={styles.evidenceRow} onClick={() => openItem(e.nodeId)} title="Open">
                <SourceLogo source={e.source} size={16} className={styles.evidenceLogo} />
                <span className={styles.evidenceLabel}>{e.label}</span>
                <span className={styles.evidenceDetail}>{e.detail}</span>
                <span className={e.quote ? styles.evidenceQuote : styles.evidenceText}>{e.quote ? `“${e.text}”` : e.text}</span>
              </button>
            </li>
          ))}
        </ul>

        <div className={styles.insightActions}>
          {insight.actions.length > 0 ? (
            insight.actions.map((a, n) => (
              <button key={a.label} type="button" className={`btn btn-sm ${n === 0 ? "btn-primary" : "btn-secondary"}`} onClick={() => apply(a.edit, a.done)}>
                {n === 0 && <SparkIcon />}
                {a.label}
              </button>
            ))
          ) : (
            <span className={styles.suggestion}>
              <SparkIcon />
              <span>
                <span className="subtle">Suggested:</span> {insight.suggestion}
              </span>
            </span>
          )}
          <span className={styles.actionsEnd}>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => openItem(insight.subject)}>
              Details
            </button>
            <Link className="btn btn-ghost btn-sm" href={`/graph?focus=${encodeURIComponent(insight.subject)}`}>
              <GraphIcon /> View in graph
            </Link>
          </span>
        </div>
      </div>
    </li>
  );
}
