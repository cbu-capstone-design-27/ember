// The dashboard (EMBER-125): the active organization's real records, read from
// `source_records` for its tenant (lib/source-records.ts). Nothing here changes
// anything, so it is a plain server component.
//
// "Needs attention" and "Work in flight" are not here: both need tickets linked
// to pull requests, which doesn't exist yet. The graph page still shows the
// preview workspace.

import Link from "next/link";
import { SourceLogo } from "../../../components/brand.tsx";
import { ArrowRightIcon } from "../../../components/icons.tsx";
import {
  MESSAGE_WINDOW_DAYS,
  type ActivityItem,
  type Breakdown,
  type Dashboard,
  type SourceCount,
} from "../../../lib/source-records.ts";
import { SOURCE_IDS, sourceInfo, type SourceId } from "../../../lib/sources.ts";
import { ago, stamp } from "../../../lib/workspace/time.ts";
import styles from "./dashboard.module.css";

/** What the page managed to read: records, a server with nowhere to read them, or a failed read. */
export type DashboardState =
  | { kind: "records"; dashboard: Dashboard }
  | { kind: "unavailable" }
  | { kind: "failed" };

function isSourceId(source: string): source is SourceId {
  return (SOURCE_IDS as readonly string[]).includes(source);
}

function sourceName(source: string): string {
  return isSourceId(source) ? sourceInfo(source).name : source;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

export function DashboardView({
  organization,
  picked,
  state,
  now,
}: {
  organization: { name: string; tenant: string };
  /** The sources the user chose in onboarding, shown on the sources card even before they have records. */
  picked: SourceId[];
  state: DashboardState;
  now: number;
}) {
  const dashboard = state.kind === "records" ? state.dashboard : null;
  const empty = dashboard !== null && dashboard.sources.length === 0;
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Dashboard</h1>
        <p className={styles.note}>
          <span>
            {organization.name} · tenant <span className="mono">{organization.tenant}</span>
          </span>
        </p>
      </header>

      {state.kind === "unavailable" && <Unavailable />}
      {state.kind === "failed" && <Failed />}
      {empty && <NoRecords organization={organization.name} tenant={organization.tenant} />}
      {dashboard && !empty && (
        <>
          <Summary dashboard={dashboard} />
          <div className={styles.columns}>
            <div className={styles.mainCol}>
              <ActivityFeed items={dashboard.activity} now={now} />
            </div>
            <div className={styles.sideCol}>
              <SourcesCard counts={dashboard.sources} picked={picked} now={now} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Unavailable() {
  return (
    <section className={styles.empty}>
      <h2>This server can't read records</h2>
      <p>
        Records live in Postgres, next to the ingestion pipeline. This copy of the app runs on a local SQLite file, so there is nothing to
        show. Point <span className="mono">DATABASE_URL</span> at the Postgres that ingestion writes to.
      </p>
    </section>
  );
}

function Failed() {
  return (
    <section className={styles.empty} role="alert">
      <h2>Couldn't read your records</h2>
      <p>The database didn't answer, or doesn't have the ingestion tables. Reload to try again; the details are in the server log.</p>
    </section>
  );
}

function NoRecords({ organization, tenant }: { organization: string; tenant: string }) {
  return (
    <section className={styles.empty}>
      <h2>No records for {organization} yet</h2>
      <p>
        Ember files everything a connected source sends under the tenant <span className="mono">{tenant}</span>. Nothing is filed there yet:
        connect a source, or wait for the first delivery to be processed. If your team's data is under another tenant, an Ember admin can
        link it in Settings.
      </p>
      <Link className="btn btn-primary" href="/settings#sources">
        Connect a source <ArrowRightIcon />
      </Link>
    </section>
  );
}

function breakdownText(b: Breakdown, none: string): string {
  const parts = Object.entries(b.bySource).map(([source, n]) => `${sourceName(source)} ${n}`);
  return parts.length > 0 ? parts.join(" · ") : none;
}

function Summary({ dashboard: { summary } }: { dashboard: Dashboard }) {
  const cards = [
    { label: "Open pull requests", value: summary.openPullRequests, none: "None open" },
    { label: "Open issues", value: summary.openIssues, none: "None open" },
    { label: `Messages, last ${MESSAGE_WINDOW_DAYS} days`, value: summary.messages, none: "No messages" },
  ];
  return (
    <section className={styles.stats} aria-label="Summary">
      {cards.map(({ label, value, none }) => (
        <div key={label} className={`${styles.stat} ${styles.statStatic}`} data-tone="neutral">
          <span className={styles.statLabel}>{label}</span>
          <span className={styles.statValue}>{value.total.toLocaleString("en-US")}</span>
          <span className={styles.statDetail}>{breakdownText(value, none)}</span>
        </div>
      ))}
    </section>
  );
}

function SourcesCard({ counts, picked, now }: { counts: SourceCount[]; picked: SourceId[]; now: number }) {
  const bySource = new Map(counts.map((c) => [c.source, c]));
  // The catalog's order, then anything else the data holds
  const order = [...SOURCE_IDS.filter((id) => bySource.has(id) || picked.includes(id)), ...counts.map((c) => c.source).filter((s) => !isSourceId(s))];
  return (
    <section className={styles.panel} aria-labelledby="sources-title">
      <div className={styles.panelHead}>
        <h2 id="sources-title">Your sources</h2>
        <Link className={styles.panelAction} href="/settings#sources">
          Manage
        </Link>
      </div>
      <ul className={styles.sourceList}>
        {order.map((source) => {
          const count = bySource.get(source);
          return (
            <li key={source}>
              <Link href="/settings#sources" className={styles.sourceRow}>
                {isSourceId(source) ? <SourceLogo source={source} size={18} /> : <span />}
                <span className={styles.sourceName}>{sourceName(source)}</span>
                <span className={styles.sourceDetail}>
                  {count ? (
                    <>
                      {plural(count.records, "record")} · <time dateTime={count.lastActivity.toISOString()} title={stamp(count.lastActivity.toISOString())}>{ago(count.lastActivity.toISOString(), now)}</time>
                    </>
                  ) : (
                    "No records yet"
                  )}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ActivityFeed({ items, now }: { items: ActivityItem[]; now: number }) {
  return (
    <section className={styles.panel} aria-labelledby="activity-title">
      <div className={styles.panelHead}>
        <h2 id="activity-title">Recent activity</h2>
      </div>
      {items.length === 0 ? (
        <p className={styles.quiet}>Nothing yet that Ember shows here: pull requests, issues, comments, reviews and messages.</p>
      ) : (
        <ol className={styles.activity}>
          {items.map((item) => (
            <li key={item.id}>
              <ActivityRow item={item} now={now} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function ActivityRow({ item, now }: { item: ActivityItem; now: number }) {
  const iso = item.at.toISOString();
  const meta = [item.label, item.target, item.status, item.actor].filter((part): part is string => !!part);
  const content = (
    <>
      <span className={styles.activityLogo}>{isSourceId(item.source) ? <SourceLogo source={item.source} size={14} /> : null}</span>
      <span className={styles.activityBody}>
        <span className={styles.activityTitle}>{item.title}</span>
        <span className={styles.activityText}>{meta.join(" · ")}</span>
        {item.excerpt && <span className={styles.activityText}>{item.excerpt}</span>}
      </span>
      <time className={styles.activityTime} dateTime={iso} title={stamp(iso)}>
        {ago(iso, now)}
      </time>
    </>
  );
  // Records come from outside Ember: the link opens in a new tab and sends no referrer
  return item.url ? (
    <a className={styles.activityItem} href={item.url} target="_blank" rel="noopener noreferrer" title={`Open in ${sourceName(item.source)}`}>
      {content}
    </a>
  ) : (
    <div className={`${styles.activityItem} ${styles.activityStatic}`}>{content}</div>
  );
}
