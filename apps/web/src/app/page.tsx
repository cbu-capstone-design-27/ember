import { Fragment } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { EmberMark, SourceLogo, Wordmark } from "../components/brand.tsx";
import { DashboardIcon, GraphIcon, SettingsIcon } from "../components/icons.tsx";
import { ClaudeLogo, CodexLogo, CursorLogo, GitHubLogo, GitLabLogo, JiraLogo, SlackLogo } from "../components/logos/index.ts";
import { AgentTabs } from "../components/story/agent-tabs.tsx";
import { agentAsks, connectStory, driftStory, featureData, miniGraph, searchPreview } from "../components/story/data.ts";
import { DriftStory } from "../components/story/drift.tsx";
import { PauseOffscreen, WhenVisible } from "../components/story/in-view.tsx";
import { MiniGraphView } from "../components/story/mini-graph.tsx";
import { Embers } from "../components/story/embers.tsx";
import storyStyles from "../components/story/story.module.css";
import { getCurrentUser } from "../lib/session.ts";
import { SOURCES, type SourceId } from "../lib/sources.ts";
import { findInsights, INSIGHT_KINDS } from "../lib/workspace/insights.ts";
import { loadPreviewWorkspace } from "../lib/workspace/load.ts";
import { buildModel } from "../lib/workspace/model.ts";
import { sourceSummaries, stats } from "../lib/workspace/summary.ts";
import { ago } from "../lib/workspace/time.ts";
import { Flame } from "./(auth)/flame.tsx";
import { AgentStack, Architecture, BeforeAfter, Faq, FeatureGrid, HowItWorks } from "./landing-sections.tsx";
import styles from "./landing.module.css";

const REPO = "https://github.com/cbu-capstone-design-27/ember";

// The sources with ingestion in the repo, and what Ember reads from each.
// Teams is left out: it is designed (docs/ingestion/teams.md) but not built.
const READS: Partial<Record<SourceId, string[]>> = {
  github: ["pull requests", "reviews", "checks"],
  gitlab: ["merge requests", "approvals", "notes"],
  jira: ["issues", "status history", "comments"],
  slack: ["channels", "threads", "decisions"],
};
const BUILT = SOURCES.filter((src) => READS[src.id]);

// The tiles that drift around the headline: [Logo, left %, top %, size rem, tilt deg, seconds].
const TILES = [
  [ClaudeLogo, 6, 24, 3.5, -8, 13],
  [GitHubLogo, 3, 58, 3, 5, 11],
  [JiraLogo, 17, 68, 3.25, -4, 15],
  [GitLabLogo, 27, 4, 2.75, 6, 12],
  [CodexLogo, 89, 10, 3.5, 7, 14],
  [SlackLogo, 93, 54, 3, -6, 12],
  [CursorLogo, 79, 70, 3.25, 4, 16],
] as const;

const PREVIEW: SourceId[] = ["github", "jira", "slack"];
const QUESTION = "saved cards";

export default async function Home() {
  const user = await getCurrentUser();
  if (user) redirect(user.sources.length ? "/dashboard" : "/onboarding");

  // Everything below is drawn from the real preview workspace.
  const { workspace, now } = loadPreviewWorkspace(PREVIEW);
  const model = buildModel(workspace);
  const insights = findInsights(model, now);
  const s = stats(model, insights);
  const summaries = sourceSummaries(model, PREVIEW);
  const graph = miniGraph();
  const features = featureData();
  const connect = connectStory();
  const hits = searchPreview(QUESTION);

  const figures = [
    { label: "Needs attention", value: s.attention.total, hot: s.attention.high > 0 },
    { label: "Open pull requests", value: s.pullRequests?.open ?? 0 },
    { label: "Tickets in flight", value: s.inFlight?.total ?? 0 },
    { label: "Messages about work", value: s.conversations?.linked ?? 0 },
  ];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link href="/" className={styles.brand} aria-label="Ember home">
          <Wordmark size={22} />
        </Link>
        <nav className={styles.headerActions} aria-label="Account">
          <Link className={styles.headerLink} href="/signup">
            Sign up
          </Link>
          <Link className={styles.headerLink} href="/login">
            Log in
          </Link>
          <a className={styles.githubPill} href={REPO}>
            <GitHubLogo size={15} />
            GitHub
          </a>
        </nav>
      </header>

      <main>
        <div className={styles.top}>
          <PauseOffscreen className={styles.tiles}>
            {TILES.map(([Logo, left, top, size, tilt, seconds], i) => (
              <span
                key={i}
                className={storyStyles.floatTile}
                aria-hidden="true"
                style={{
                  left: `${left}%`,
                  top: `${top}%`,
                  ["--tile" as string]: `${size}rem`,
                  ["--tilt" as string]: `${tilt}deg`,
                  animationDuration: `${seconds}s`,
                  animationDelay: `${-i * 1.7}s`,
                }}
              >
                <Logo size={Math.round(size * 8.5)} />
              </span>
            ))}
          </PauseOffscreen>

          <section className={styles.hero}>
            <h1 className={styles.title}>The why behind your code, for every coding agent.</h1>
            <p className={styles.lede}>
              Ember turns your team&apos;s GitHub, Jira and Slack history into a knowledge graph of decisions, tickets,
              people and code, and serves it to Claude&nbsp;Code, Codex and Cursor over MCP.
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

          <AgentStack asks={agentAsks()} />
        </div>

        <section id="sources" className={styles.section}>
          <h2 className={styles.sectionTitle}>Reads the tools you already use</h2>
          <ul className={styles.sources}>
            {BUILT.map((src) => (
              <li key={src.id}>
                <SourceLogo source={src.id} size={22} />
                <span>
                  <span className={styles.sourceName}>{src.name}</span>
                  <code>
                    {READS[src.id]!.map((r, i, all) =>
                      i < all.length - 1 ? (
                        <Fragment key={r}>
                          <span>{r} ·</span>{" "}
                        </Fragment>
                      ) : (
                        <span key={r}>{r}</span>
                      ),
                    )}
                  </code>
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section id="how" className={styles.section}>
          <h2 className={styles.sectionTitle}>Running in three steps</h2>
          <HowItWorks
            org={model.ws.organization.name}
            tenant={model.ws.organization.tenant}
            people={model.ws.people.map((p) => p.name)}
            sources={connect.sources.map((src) => ({ id: src.id, name: SOURCES.find((x) => x.id === src.id)!.name, status: src.status }))}
          />
        </section>

        <section id="agents" className={`${styles.section} ${styles.split}`}>
          <div className={styles.splitText}>
            <h2 className={styles.sectionTitle}>Works in your agent</h2>
            <p className={styles.sectionLede}>
              Ember&apos;s MCP server gives Claude Code, Codex and Cursor two read-only tools, <code>search</code> and{" "}
              <code>get_record</code>, over everything Ember has ingested.
            </p>
          </div>
          <AgentTabs question={QUESTION} hits={hits} />
        </section>

        <section id="features" className={styles.section}>
          <h2 className={styles.sectionTitle}>Context your agent can actually use</h2>
          <FeatureGrid data={features} hit={hits[0]} />
        </section>

        <section id="drift" className={`${styles.section} ${styles.split}`}>
          <div className={styles.splitText}>
            <h2 className={styles.sectionTitle}>Catches the drift</h2>
            <p className={styles.sectionLede}>
              Slack says CHK-131 is done and the pull request merged, but Jira still says In Development. Ember links the
              three records and raises the flag, with a one-click fix.
            </p>
          </div>
          <div className={styles.driftStage}>
            <DriftStory drift={driftStory()} />
          </div>
        </section>

        <div className={styles.frameWrap}>
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

        <section id="graph" className={styles.section}>
          <h2 className={styles.sectionTitle}>One graph under all of it</h2>
          <div className={styles.graphRow}>
            <div className={styles.graphPanel}>
              <div className={styles.graphBar}>
                <code>{model.ws.organization.tenant} / CHK-131</code>
                <code>
                  {features.counts.nodes} nodes · {features.counts.edges} edges
                </code>
              </div>
              <MiniGraphView graph={graph} />
            </div>
            <div className={styles.stream}>
              <div className={styles.graphBar}>
                <span>Activity</span>
              </div>
              <ol>
                {features.activity.map((a) => (
                  <li key={a.id}>
                    <SourceLogo source={a.source} size={13} />
                    <span>
                      <b>{a.actor}</b> {a.action} <code>{a.target}</code>
                    </span>
                    <code className={styles.streamWhen}>{a.when}</code>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </section>

        <section id="architecture" className={styles.section}>
          <h2 className={styles.sectionTitle}>From webhook to agent</h2>
          <Architecture />
        </section>

        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>What changes</h2>
          <BeforeAfter />
        </section>

        <section id="faq" className={styles.section}>
          <h2 className={styles.sectionTitle}>Questions</h2>
          <Faq />
        </section>
      </main>

      <section className={styles.closing}>
        <WhenVisible className={styles.closingFire}>
          <Flame />
        </WhenVisible>
        <Embers fire={0.45} toTop className={styles.pageEmbers} />
        <div className={styles.closingBody}>
          <h2 className={styles.closingTitle}>Give your agent the why.</h2>
          <p>Try it on the preview workspace. Nothing to connect yet.</p>
          <div className={styles.cta}>
            <Link className="btn btn-primary btn-lg" href="/signup">
              Create an account
            </Link>
            <Link className="btn btn-secondary btn-lg" href="/login">
              Log in
            </Link>
          </div>
        </div>
      </section>

      <footer className={styles.footer}>
        <div className={styles.footerBrand}>
          <Wordmark size={20} />
        </div>
        <nav className={styles.footerCols} aria-label="Footer">
          <div>
            <h3>Product</h3>
            <Link href="/signup">Create an account</Link>
            <Link href="/login">Log in</Link>
            <Link href="/device">CLI sign-in</Link>
          </div>
          <div>
            <h3>Sources</h3>
            {BUILT.map((src) => (
              <a key={src.id} href="#sources">
                {src.name}
              </a>
            ))}
          </div>
          <div>
            <h3>Agents</h3>
            <a href="#agents">Claude Code</a>
            <a href="#agents">Codex</a>
            <a href="#agents">Cursor</a>
          </div>
          <div>
            <h3>Project</h3>
            <a href={REPO}>GitHub</a>
            <a href={`${REPO}/blob/main/LICENSE`}>Apache-2.0</a>
          </div>
        </nav>
      </footer>
    </div>
  );
}
