import type { Metadata } from "next";
import Link from "next/link";
import { Avatar } from "../../../components/avatar.tsx";
import { SourceLogo } from "../../../components/brand.tsx";
import {
  ActivityIcon,
  ArrowRightIcon,
  CheckIcon,
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
import { requireOnboardedUser } from "../../../lib/session.ts";
import { sourceInfo, type SourceId } from "../../../lib/sources.ts";
import { INSIGHT_KINDS, type InsightKind } from "../../../lib/workspace/insights.ts";
import { loadWorkspace } from "../../../lib/workspace/load.ts";
import { activity, sourceSummaries, stats, workInFlight, type ActivityItem, type Stats, type WorkRow } from "../../../lib/workspace/summary.ts";
import { ago, stamp } from "../../../lib/workspace/time.ts";
import styles from "./dashboard.module.css";
import { InsightsPanel } from "./insights.tsx";

export const metadata: Metadata = { title: "Dashboard" };

export default async function DashboardPage() {
  const user = await requireOnboardedUser("/dashboard");
  const { model, insights, now } = loadWorkspace(user.sources);
  const firstName = user.name.split(/\s+/)[0] || user.name;
  const withData = model.sources;
  const kinds = (Object.entries(INSIGHT_KINDS) as Array<[InsightKind, { label: string; order: number }]>)
    .sort((a, b) => a[1].order - b[1].order)
    .map(([kind, { label }]) => ({ kind, label }));

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
          <span className="badge badge-accent">
            <span className="dot" /> Preview data
          </span>
          <span className="subtle">Updated {ago(new Date(now).toISOString(), now)}</span>
        </div>
      </header>

      <div className={`alert alert-info ${styles.banner}`}>
        <InfoIcon />
        <span>
          This is <b>{model.ws.organization.name}</b>, a made-up team, so you can see what Ember does before anything is
          connected. Your own work shows up here once your sources are connected.
        </span>
      </div>

      {withData.length === 0 ? (
        <NoPreviewData sources={user.sources} />
      ) : (
        <>
          <StatCards stats={stats(model, insights)} />
          <div className={styles.columns}>
            <div className={styles.mainCol}>
              <InsightsPanel insights={insights} kinds={kinds} now={now} />
              {model.ws.jira && <WorkTable rows={workInFlight(model, insights)} now={now} />}
            </div>
            <div className={styles.sideCol}>
              <SourcesCard summaries={sourceSummaries(model, user.sources)} />
              <ActivityFeed items={activity(model, 14)} now={now} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function listOf(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
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

function StatCards({ stats: s }: { stats: Stats }) {
  const cards = [
    {
      Icon: SparkIcon,
      label: "Needs attention",
      value: s.attention.total,
      detail: s.attention.high ? `${s.attention.high} high priority` : "Nothing urgent",
      tone: s.attention.high ? "danger" : "neutral",
      missing: null,
    },
    {
      Icon: PullRequestIcon,
      label: "Open pull requests",
      value: s.pullRequests?.open,
      detail: s.pullRequests && `${s.pullRequests.awaitingReview} waiting for review · ${s.pullRequests.drafts} drafts`,
      tone: "neutral",
      missing: "GitHub",
    },
    {
      Icon: TicketIcon,
      label: "Tickets in flight",
      value: s.inFlight?.total,
      detail: s.inFlight && `${s.inFlight.inReview} in review`,
      tone: "neutral",
      missing: "Jira",
    },
    {
      Icon: MessageIcon,
      label: "Messages about work",
      value: s.conversations?.linked,
      detail: s.conversations && `of ${s.conversations.total} mention a ticket or PR`,
      tone: "neutral",
      missing: "Slack",
    },
  ];
  return (
    <section className={styles.stats} aria-label="Summary">
      {cards.map(({ Icon, label, value, detail, tone, missing }) => (
        <div key={label} className={`card ${styles.stat}`} data-tone={tone}>
          <span className={styles.statIcon}>
            <Icon />
          </span>
          <span className={styles.statLabel}>{label}</span>
          {value === undefined ? (
            <>
              <span className={`${styles.statValue} subtle`}>—</span>
              <span className={styles.statDetail}>Add {missing} to see this</span>
            </>
          ) : (
            <>
              <span className={styles.statValue}>{value}</span>
              <span className={styles.statDetail}>{detail}</span>
            </>
          )}
        </div>
      ))}
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

function WorkTable({ rows, now }: { rows: WorkRow[]; now: number }) {
  return (
    <section className={`card ${styles.panel}`} aria-labelledby="work-title">
      <div className="card-header">
        <div>
          <h2 id="work-title">Work in flight</h2>
          <p>Every ticket in progress or review, with its pull request and where it was last discussed.</p>
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
              <tr key={row.key}>
                <td>
                  <Link href={`/graph?focus=${encodeURIComponent(row.nodeId)}`} className={styles.ticket}>
                    <span className={styles.ticketKey}>
                      {row.key}
                      {row.insights > 0 && (
                        <span className={styles.flag} title={`${row.insights} insight${row.insights > 1 ? "s" : ""}`}>
                          {row.insights}
                        </span>
                      )}
                    </span>
                    <span className={styles.ticketSummary}>{row.summary}</span>
                  </Link>
                </td>
                <td>
                  <span className={STAGE_BADGE[row.stage]}>{row.status}</span>
                </td>
                <td>{row.change ? <ChangeCell change={row.change} /> : <span className="subtle">None linked</span>}</td>
                <td className={styles.hideNarrow}>
                  {row.lastMention ? (
                    <span className={styles.mention} title={stamp(row.lastMention.at)}>
                      <SourceLogo source="slack" size={14} />
                      <span>#{row.lastMention.channel}</span>
                      <span className={styles.mentionTime}>{ago(row.lastMention.at, now)}</span>
                    </span>
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

function SourcesCard({ summaries }: { summaries: ReturnType<typeof sourceSummaries> }) {
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
        {summaries.map(({ id, detail }) => (
          <li key={id}>
            <span className={styles.sourceLogo}>
              <SourceLogo source={id} size={20} />
            </span>
            <span className={styles.sourceText}>
              <span className={styles.sourceName}>{sourceInfo(id).name}</span>
              <span className="subtle">{detail ?? "No preview data yet"}</span>
            </span>
            {detail ? <span className="badge badge-accent">Preview</span> : <span className="badge">Later</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function ActivityFeed({ items, now }: { items: ActivityItem[]; now: number }) {
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
            <span className={styles.activityLogo}>
              <SourceLogo source={item.source} size={14} />
            </span>
            <div className={styles.activityBody}>
              <p>
                <b>{item.actor}</b> {item.action}{" "}
                <Link href={`/graph?focus=${encodeURIComponent(item.nodeId)}`} className={styles.activityTarget}>
                  {item.target}
                </Link>
              </p>
              {item.text && <p className={styles.activityText}>{item.text}</p>}
            </div>
            <time className={styles.activityTime} dateTime={item.at} title={stamp(item.at)}>
              {ago(item.at, now)}
            </time>
          </li>
        ))}
      </ol>
    </section>
  );
}
