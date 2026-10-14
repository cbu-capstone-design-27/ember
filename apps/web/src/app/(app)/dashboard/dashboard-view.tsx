"use client";

// The dashboard. Everything on it is live against the preview workspace:
// numbers open the items behind them, rows and activity open the drawer, and
// insight actions change the data (see components/preview/store.tsx).

import Link from "next/link";
import { useMemo } from "react";
import { Avatar } from "../../../components/avatar.tsx";
import { SourceLogo } from "../../../components/brand.tsx";
import {
  ActivityIcon,
  ArrowRightIcon,
  CheckIcon,
  ChevronRightIcon,
  DraftIcon,
  InfoIcon,
  LayersIcon,
  MergeIcon,
  MessageIcon,
  PullRequestIcon,
  SparkIcon,
  TicketIcon,
  XIcon,
} from "../../../components/icons.tsx";
import { usePreview } from "../../../components/preview/store.tsx";
import { sourceInfo, type SourceId } from "../../../lib/sources.ts";
import { changeId, contextRefs, nodeId } from "../../../lib/workspace/model.ts";
import { activity, sourceSummaries, stats, workInFlight, type WorkRow } from "../../../lib/workspace/summary.ts";
import { ago, stamp } from "../../../lib/workspace/time.ts";
import styles from "./dashboard.module.css";
import { InsightsPanel } from "./insights.tsx";

function listOf(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

export function DashboardView({ firstName, sources }: { firstName: string; sources: SourceId[] }) {
  const { model, insights, now, editCount, reset } = usePreview();
  const withData = model.sources;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>Welcome back, {firstName}</h1>
          <p className="muted">
            {withData.length
              ? `What moved across ${listOf(withData.map((s) => sourceInfo(s).name))} at ${model.ws.organization.name}, and where it doesn't add up.`
              : "Your sources don't have preview data yet."}
          </p>
        </div>
        <div className={styles.headerMeta}>
          <Link className="badge badge-accent" href="/settings#preview-data" title="About preview data">
            <span className="dot" /> Preview data
          </Link>
          <span className="subtle">Updated {ago(new Date(now).toISOString(), now)}</span>
        </div>
      </header>

      <div className={`alert alert-info ${styles.banner}`}>
        <InfoIcon />
        <span>
          This is <b>{model.ws.organization.name}</b>, a made-up team, so you can try Ember before anything is connected.
          Everything works: click any item, or take a suggested action.{" "}
          {editCount > 0 ? (
            <>
              You&apos;ve made {editCount} change{editCount === 1 ? "" : "s"}, saved in this browser.{" "}
              <button type="button" className={styles.inlineLink} onClick={reset}>
                Start over
              </button>
            </>
          ) : (
            "Changes you make stay in this browser."
          )}
        </span>
      </div>

      {withData.length === 0 ? (
        <NoPreviewData sources={sources} />
      ) : (
        <>
          <StatCards />
          <div className={styles.columns}>
            <div className={styles.mainCol}>
              <InsightsPanel />
              {model.ws.jira && <WorkTable rows={workInFlight(model, insights)} />}
            </div>
            <div className={styles.sideCol}>
              <SourcesCard sources={sources} />
              <ActivityFeed />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function NoPreviewData({ sources }: { sources: SourceId[] }) {
  return (
    <section className={`card ${styles.empty}`}>
      <LayersIcon />
      <h2>No preview data for {listOf(sources.map((s) => sourceInfo(s).name))} yet</h2>
      <p className="muted">
        The preview workspace has GitHub, Jira and Slack data. Add one of those to your sources to explore the dashboard
        and the graph while the other connectors are built.
      </p>
      <Link className="btn btn-primary" href="/settings">
        Manage sources <ArrowRightIcon />
      </Link>
    </section>
  );
}

function StatCards() {
  const { model, insights, open } = usePreview();
  const s = stats(model, insights);
  const openPrs = [...model.changes.values()]
    .filter((p) => p.state === "open" || p.state === "draft")
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
    .map((p) => nodeId.change(changeId(p)));
  const linkedMessages = [...model.messages.values()]
    .filter((m) => {
      const r = contextRefs(model, m);
      return r.issues.length > 0 || r.changes.length > 0;
    })
    .sort((a, b) => b.at.localeCompare(a.at))
    .map((m) => nodeId.message(m.ts));

  const cards = [
    {
      Icon: SparkIcon,
      label: "Needs attention",
      value: s.attention.total,
      detail: s.attention.high ? `${s.attention.high} high priority` : "Nothing urgent",
      tone: s.attention.high ? "danger" : "neutral",
      missing: null,
      onClick: () => scrollTo("attention"),
      hint: "Go to the list",
    },
    {
      Icon: PullRequestIcon,
      label: "Open pull requests",
      value: s.pullRequests?.open,
      detail: s.pullRequests && `${s.pullRequests.awaitingReview} waiting for review · ${s.pullRequests.drafts} drafts`,
      tone: "neutral",
      missing: "GitHub",
      onClick: () => open({ kind: "list", title: "Open pull requests", ids: openPrs }),
      hint: "See them",
    },
    {
      Icon: TicketIcon,
      label: "Tickets in flight",
      value: s.inFlight?.total,
      detail: s.inFlight && `${s.inFlight.inReview} in review`,
      tone: "neutral",
      missing: "Jira",
      onClick: () => scrollTo("work"),
      hint: "Go to the table",
    },
    {
      Icon: MessageIcon,
      label: "Messages about work",
      value: s.conversations?.linked,
      detail: s.conversations && `of ${s.conversations.total} mention a ticket or PR`,
      tone: "neutral",
      missing: "Slack",
      onClick: () => open({ kind: "list", title: "Messages about work", ids: linkedMessages }),
      hint: "Read them",
    },
  ];
  return (
    <section className={styles.stats} aria-label="Summary">
      {cards.map(({ Icon, label, value, detail, tone, missing, onClick, hint }) =>
        value === undefined ? (
          <Link key={label} href="/settings" className={`card ${styles.stat}`} data-tone="neutral">
            <span className={styles.statIcon}>
              <Icon />
            </span>
            <span className={styles.statLabel}>{label}</span>
            <span className={`${styles.statValue} subtle`}>—</span>
            <span className={styles.statDetail}>Add {missing} to see this</span>
          </Link>
        ) : (
          <button key={label} type="button" className={`card ${styles.stat}`} data-tone={tone} onClick={onClick}>
            <span className={styles.statIcon}>
              <Icon />
            </span>
            <span className={styles.statLabel}>{label}</span>
            <span className={styles.statValue}>{value}</span>
            <span className={styles.statDetail}>{detail}</span>
            <span className={styles.statHint}>
              {hint} <ChevronRightIcon />
            </span>
          </button>
        ),
      )}
    </section>
  );
}

const STAGE_BADGE: Record<WorkRow["stage"], string> = {
  todo: "badge",
  in_progress: "badge badge-info",
  in_review: "badge badge-warning",
  done: "badge badge-success",
};

function ChangeCell({ change }: { change: NonNullable<WorkRow["change"]> }) {
  const Icon = change.state === "merged" ? MergeIcon : change.state === "draft" ? DraftIcon : PullRequestIcon;
  const review = {
    approved: "Approved",
    changes_requested: "Changes requested",
    waiting: "Waiting for review",
    none: change.state === "draft" ? "Draft" : "No reviewers",
  }[change.review];
  return (
    <span className={styles.changeCell}>
      <span className={styles.changeState} data-state={change.state}>
        <Icon />
      </span>
      <span className={styles.changeText}>
        <span className="mono">{change.id}</span>
        <span className={styles.changeMeta}>
          {change.state === "merged" ? "Merged" : review}
          {change.state !== "merged" && change.checks !== "pending" && (
            <span className={styles.checks} data-checks={change.checks} title={`Checks ${change.checks}`}>
              {change.checks === "passing" ? <CheckIcon /> : <XIcon />}
            </span>
          )}
        </span>
      </span>
    </span>
  );
}

function WorkTable({ rows }: { rows: WorkRow[] }) {
  const { model, now, openItem } = usePreview();
  const lastMention = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of rows) {
      const m = [...model.messages.values()]
        .filter((x) => contextRefs(model, x).issues.includes(row.key))
        .sort((a, b) => b.at.localeCompare(a.at))[0];
      if (m) map.set(row.key, nodeId.message(m.ts));
    }
    return map;
  }, [rows, model]);

  return (
    <section id="work" className={`card ${styles.panel}`} aria-labelledby="work-title">
      <div className="card-header">
        <div>
          <h2 id="work-title">Work in flight</h2>
          <p>Every ticket in progress or review, with its pull request and where it was last discussed. Click a row to open it.</p>
        </div>
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Ticket</th>
              <th scope="col">Status</th>
              <th scope="col">Pull request</th>
              <th scope="col" className={styles.hideNarrow}>
                Last discussed
              </th>
              <th scope="col" className={styles.hideNarrow}>
                <span className="visually-hidden">Assignee</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className={styles.clickableRow} onClick={(e) => !(e.target as Element).closest("button") && openItem(row.nodeId)}>
                <td>
                  <button type="button" className={styles.ticket} onClick={() => openItem(row.nodeId)}>
                    <span className={styles.ticketKey}>
                      {row.key}
                      {row.insights > 0 && (
                        <span className={styles.flag} title={`${row.insights} insight${row.insights > 1 ? "s" : ""}`}>
                          {row.insights}
                        </span>
                      )}
                    </span>
                    <span className={styles.ticketSummary}>{row.summary}</span>
                  </button>
                </td>
                <td>
                  <span className={STAGE_BADGE[row.stage]}>{row.status}</span>
                </td>
                <td>
                  {row.change ? (
                    <button type="button" className={styles.cellButton} onClick={() => openItem(nodeId.change(row.change!.id))}>
                      <ChangeCell change={row.change} />
                    </button>
                  ) : (
                    <span className="subtle">None linked</span>
                  )}
                </td>
                <td className={styles.hideNarrow}>
                  {row.lastMention ? (
                    <button
                      type="button"
                      className={`${styles.cellButton} ${styles.mention}`}
                      title={stamp(row.lastMention.at)}
                      onClick={() => openItem(lastMention.get(row.key)!)}
                    >
                      <SourceLogo source="slack" size={14} />
                      <span>#{row.lastMention.channel}</span>
                      <span className={styles.mentionTime}>{ago(row.lastMention.at, now)}</span>
                    </button>
                  ) : (
                    <span className="subtle">Not discussed</span>
                  )}
                </td>
                <td className={styles.hideNarrow}>
                  <Avatar name={row.assignee} size="sm" title={row.assignee} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function SourcesCard({ sources }: { sources: SourceId[] }) {
  const { model, openItem } = usePreview();
  const summaries = sourceSummaries(model, sources);
  // Each source opens its main container: the project, the busiest repo or channel.
  const target = (id: SourceId): string | null => {
    if (id === "jira" && model.ws.jira) return nodeId.project(model.ws.jira.project.key);
    if (id === "github" && model.ws.github) return nodeId.repository(model.ws.github.repositories[0].name);
    if (id === "slack" && model.ws.slack) return nodeId.channel(model.ws.slack.channels[0].id);
    return null;
  };
  return (
    <section className={`card ${styles.panel}`} aria-labelledby="sources-title">
      <div className="card-header">
        <div>
          <h2 id="sources-title">Your sources</h2>
          <p>What this dashboard reads.</p>
        </div>
        <Link className="btn btn-ghost btn-sm" href="/settings">
          Manage
        </Link>
      </div>
      <ul className={styles.sourceList}>
        {summaries.map(({ id, detail }) => {
          const to = target(id);
          const content = (
            <>
              <span className={styles.sourceLogo}>
                <SourceLogo source={id} size={20} />
              </span>
              <span className={styles.sourceText}>
                <span className={styles.sourceName}>{sourceInfo(id).name}</span>
                <span className="subtle">{detail ?? "No preview data yet"}</span>
              </span>
              {detail ? <span className="badge badge-accent">Preview</span> : <span className="badge">Later</span>}
            </>
          );
          return (
            <li key={id}>
              {to ? (
                <button type="button" className={styles.sourceRow} onClick={() => openItem(to)}>
                  {content}
                </button>
              ) : (
                <Link href="/settings" className={styles.sourceRow}>
                  {content}
                </Link>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ActivityFeed() {
  const { model, now, openItem } = usePreview();
  const items = activity(model, 14);
  return (
    <section className={`card ${styles.panel}`} aria-labelledby="activity-title">
      <div className="card-header">
        <div>
          <h2 id="activity-title">Recent activity</h2>
          <p>Across every source, newest first.</p>
        </div>
        <ActivityIcon className={`icon ${styles.headerIcon}`} />
      </div>
      <ol className={styles.activity}>
        {items.map((item) => (
          <li key={item.id}>
            <button type="button" className={styles.activityItem} onClick={() => openItem(item.nodeId)}>
              <span className={styles.activityLogo}>
                <SourceLogo source={item.source} size={14} />
              </span>
              <span className={styles.activityBody}>
                <span className={styles.activityLine}>
                  <b>{item.actor}</b> {item.action} <span className={styles.activityTarget}>{item.target}</span>
                </span>
                {item.text && <span className={styles.activityText}>{item.text}</span>}
              </span>
              <time className={styles.activityTime} dateTime={item.at} title={stamp(item.at)}>
                {ago(item.at, now)}
              </time>
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}
