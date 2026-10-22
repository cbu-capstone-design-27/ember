import Link from "next/link";
import { redirect } from "next/navigation";
import { EmberMark, SourceLogo, Wordmark } from "../components/brand.tsx";
import { DashboardIcon, GraphIcon, SettingsIcon } from "../components/icons.tsx";
import { ThemeSwitcher } from "../components/theme.tsx";
import { getCurrentUser } from "../lib/session.ts";
import { SOURCES, type SourceId } from "../lib/sources.ts";
import { buildGraph } from "../lib/workspace/graph.ts";
import { findInsights, INSIGHT_KINDS } from "../lib/workspace/insights.ts";
import { loadPreviewWorkspace } from "../lib/workspace/load.ts";
import { buildModel, nodeId } from "../lib/workspace/model.ts";
import { sourceSummaries, stats } from "../lib/workspace/summary.ts";
import { ago } from "../lib/workspace/time.ts";
import styles from "./landing.module.css";

// What each source adds to the graph, in the ontology's node types (docs/graph-schema.md).
const NODE_TYPES_BY_SOURCE: Record<SourceId, string> = {
  github: "Change · Module",
  gitlab: "Change · Module",
  jira: "WorkItem",
  slack: "Message · Decision",
  teams: "Message · Decision",
};

const PREVIEW_SOURCES: SourceId[] = ["github", "jira", "slack"];

export default async function Home() {
  const user = await getCurrentUser();
  if (user) redirect(user.sources.length ? "/dashboard" : "/onboarding");

  // The hero panel and the graph excerpt render the real preview workspace.
  const { workspace, now } = loadPreviewWorkspace(PREVIEW_SOURCES);
  const model = buildModel(workspace);
  const insights = findInsights(model, now);
  const s = stats(model, insights);
  const summaries = sourceSummaries(model, PREVIEW_SOURCES);
  const graph = buildGraph(model, now);
  const focus = nodeId.issue("CHK-131");
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const edges = graph.edges
    .filter((e) => e.source === focus || e.target === focus)
    .map((e) => {
      const out = e.source === focus;
      return { id: e.id, type: e.type, out, other: byId.get(out ? e.target : e.source)! };
    })
    .slice(0, 7);

  const figures = [
    { label: "Needs attention", value: s.attention.total, hot: s.attention.high > 0 },
    { label: "Open pull requests", value: s.pullRequests?.open ?? 0 },
    { label: "Tickets in flight", value: s.inFlight?.total ?? 0 },
    { label: "Messages about work", value: s.conversations?.linked ?? 0 },
  ];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Wordmark size={22} />
        <nav className={styles.headerActions} aria-label="Account">
          <Link className={styles.headerLink} href="/login">
            Log in
          </Link>
          <Link className="btn btn-primary btn-sm" href="/signup">
            Get started
          </Link>
        </nav>
      </header>

      <main>
        <div className={styles.top}>
          <section className={styles.hero}>
            <h1 className={styles.title}>See where GitHub, Jira and Slack disagree.</h1>
            <p className={styles.lede}>
              Ember links pull requests, tickets and threads into one graph and flags work whose status doesn&apos;t match.
            </p>
            <div className={styles.cta}>
              <Link className="btn btn-primary btn-lg" href="/signup">
                Create an account
              </Link>
              <Link className="btn btn-secondary btn-lg" href="/login">
                Log in
              </Link>
            </div>
          </section>

          {/* The dashboard, drawn from the preview workspace. Decorative here: the real one is behind sign-in. */}
          <div className={styles.frame} aria-hidden="true">
            <div className={styles.frameBar}>
              <EmberMark size={14} />
              <span>{model.ws.organization.name}</span>
              <code>/dashboard</code>
            </div>
            <div className={styles.frameBody}>
              <div className={styles.frameSide}>
                <span className={styles.frameNav} data-active="true">
                  <DashboardIcon /> Dashboard
                </span>
                <span className={styles.frameNav}>
                  <GraphIcon /> Knowledge graph
                </span>
                <span className={styles.frameNav}>
                  <SettingsIcon /> Settings
                </span>
                <span className={styles.frameSources}>
                  {summaries.map((src) => (
                    <span key={src.id}>
                      <SourceLogo source={src.id} size={13} />
                      {SOURCES.find((x) => x.id === src.id)!.name}
                    </span>
                  ))}
                </span>
              </div>
              <div className={styles.frameMain}>
                <div className={styles.frameFigures}>
                  {figures.map((f) => (
                    <span key={f.label} data-hot={f.hot}>
                      <span>{f.label}</span>
                      <b>{f.value}</b>
                    </span>
                  ))}
                </div>
                <ol className={styles.frameList}>
                  {insights.slice(0, 6).map((i) => (
                    <li key={i.id} data-severity={i.severity}>
                      <span className={styles.frameKind}>{INSIGHT_KINDS[i.kind].label}</span>
                      <span className={styles.frameTitle}>{i.title}</span>
                      <code className={styles.frameKeys}>
                        {i.evidence
                          .slice(0, 2)
                          .map((e) => e.label)
                          .join("  ")}
                      </code>
                      <code className={styles.frameTime}>{ago(i.at, now)}</code>
                    </li>
                  ))}
                </ol>
              </div>
            </div>
          </div>
        </div>

        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Connect the tools you already use</h2>
          <ul className={styles.sources}>
            {SOURCES.map((src) => (
              <li key={src.id}>
                <SourceLogo source={src.id} size={20} />
                <span>
                  <span className={styles.sourceName}>{src.name}</span>
                  <code>{NODE_TYPES_BY_SOURCE[src.id]}</code>
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>One graph under all of it</h2>
          <div className={styles.terminal}>
            <div className={styles.terminalBar}>
              <code>{byId.get(focus)!.subgraph} / CHK-131</code>
            </div>
            <ol className={styles.terminalBody}>
              {edges.map((e) => (
                <li key={e.id}>
                  <code className={styles.edgeDir}>{e.out ? "→" : "←"}</code>
                  <code className={styles.edgeType}>{e.type}</code>
                  <code className={styles.edgeType2}>{e.other.type}</code>
                  <span className={styles.edgeLabel}>{e.other.label}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>
      </main>

      <footer className={styles.footer}>
        <span>Ember · Apache-2.0</span>
        <ThemeSwitcher />
      </footer>
    </div>
  );
}
