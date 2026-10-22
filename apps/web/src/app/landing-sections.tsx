// The landing page's content sections. Every artifact is drawn from the
// preview workspace or from what the repo really runs (docs/ingestion,
// ADR 0001 and 0002, apps/mcp); nothing here is a mock of a feature that
// doesn't exist. Planned pieces say so.

import type { ReactNode } from "react";
import { Avatar } from "../components/avatar.tsx";
import { EmberMark, SourceLogo } from "../components/brand.tsx";
import { ClaudeLogo, CodexLogo, CursorLogo } from "../components/logos/index.ts";
import type { AgentAsk, FeatureData } from "../components/story/data.ts";
import type { SourceId } from "../lib/sources.ts";
import styles from "./landing.module.css";

const AGENT = {
  cursor: { name: "Cursor", Logo: CursorLogo },
  claude: { name: "Claude Code", Logo: ClaudeLogo },
  codex: { name: "Codex", Logo: CodexLogo },
} as const;

/** Bold the **matches** in an MCP search snippet. */
function Snippet({ text }: { text: string }) {
  return <>{text.split(/\*\*(.+?)\*\*/g).map((part, i) => (i % 2 ? <b key={i}>{part}</b> : part))}</>;
}

/** Three agents answering a teammate's "why" question with Ember's MCP `search`. */
export function AgentStack({ asks }: { asks: AgentAsk[] }) {
  return (
    <ol className={styles.askStack} aria-label="Agents asking Ember">
      {asks.map((a, i) => {
        const { name, Logo } = AGENT[a.agent];
        return (
          <li key={a.agent} className={styles.ask} style={{ ["--i" as string]: i }}>
            <div className={styles.askBar}>
              <Logo size={14} />
              <span>{name}</span>
              <code>ember · search</code>
            </div>
            <div className={styles.askBody}>
              <p className={styles.askWho}>{a.who} asked</p>
              <p className={styles.askQ}>{a.question}</p>
              <p className={styles.askA}>
                <code>
                  search(&quot;{a.query}&quot;) → {a.count} {a.count === 1 ? "record" : "records"}
                </code>
                {a.top && (
                  <span className={styles.askTop}>
                    <SourceLogo source={a.top.source} size={12} />
                    <code>{a.top.label}</code>
                    <span>
                      <Snippet text={a.top.title} />
                    </span>
                  </span>
                )}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

const STATUS_LABEL = { linked: "Preview data", "no preview data": "No preview data", coming: "Coming" } as const;

/** Sign up, pick sources, connect an agent: the real steps, each with its artifact. */
export function HowItWorks({
  org,
  tenant,
  people,
  sources,
}: {
  org: string;
  tenant: string;
  people: string[];
  sources: Array<{ id: SourceId; name: string; status: keyof typeof STATUS_LABEL }>;
}) {
  return (
    <ol className={styles.steps}>
      <li className={styles.step}>
        <code className={styles.stepNum}>1</code>
        <h3>Create an account</h3>
        <p>Email and password. Until your sources connect, you explore a preview workspace.</p>
        <div className={styles.artifact}>
          <div className={styles.workspace}>
            <EmberMark size={20} />
            <span className={styles.workspaceName}>{org}</span>
            <code>{tenant}</code>
          </div>
          <div className={styles.members}>
            {people.map((p) => (
              <Avatar key={p} name={p} size="sm" />
            ))}
            <span>{people.length} people</span>
          </div>
        </div>
      </li>
      <li className={styles.step}>
        <code className={styles.stepNum}>2</code>
        <h3>Pick your sources</h3>
        <p>Choose the tools your team works in. The dashboard and graph read only those.</p>
        <ul className={`${styles.artifact} ${styles.integrations}`}>
          {sources.map((s) => (
            <li key={s.id} data-status={s.status}>
              <SourceLogo source={s.id} size={16} />
              <span>{s.name}</span>
              <code>{STATUS_LABEL[s.status]}</code>
            </li>
          ))}
        </ul>
      </li>
      <li className={styles.step}>
        <code className={styles.stepNum}>3</code>
        <h3>Connect your agent</h3>
        <p>Add Ember&apos;s MCP server to Claude Code or Codex. It runs on stdio and only reads.</p>
        <pre className={`${styles.artifact} ${styles.term}`}>
          <code>
            <span className={styles.prompt}>$</span> claude mcp add ember \{"\n"}
            {"    "}-e DATABASE_URL=&apos;postgresql://…&apos; \{"\n"}
            {"    "}-- python -m ember_mcp{"\n"}
            <span className={styles.prompt}>$</span> codex mcp add ember \{"\n"}
            {"    "}--env DATABASE_URL=&apos;postgresql://…&apos; \{"\n"}
            {"    "}-- python -m ember_mcp
          </code>
        </pre>
      </li>
    </ol>
  );
}

function Feature({ title, text, children }: { title: string; text: string; children: ReactNode }) {
  return (
    <article className={styles.feature}>
      <div className={styles.featureArt}>{children}</div>
      <h3>{title}</h3>
      <p>{text}</p>
    </article>
  );
}

/** Six features, each shown with a small real artifact instead of an icon. */
export function FeatureGrid({ data, hit }: { data: FeatureData; hit: { source: string; kind: string; object_id: string; title: string } }) {
  return (
    <div className={styles.features}>
      <Feature title="Decisions stay with the work" text="A decision made in Slack is linked to the tickets it's about, so an agent can find why, not just what.">
        <div className={styles.decision}>
          <div className={styles.decisionHead}>
            <SourceLogo source="slack" size={12} />
            <code>{data.decision.where}</code>
            <span>{data.decision.who}</span>
          </div>
          <p>{data.decision.text}</p>
          <div className={styles.decisionKeys}>
            {data.decision.issues.map((k) => (
              <code key={k}>{k}</code>
            ))}
          </div>
        </div>
      </Feature>

      <Feature title="Status drift, with the evidence" text="When the ticket, the pull request and the thread disagree, Ember shows all three and the fix.">
        <ol className={styles.timeline}>
          {data.timeline.map((t, i) => (
            <li key={i} data-hot={t.hot}>
              <SourceLogo source={t.source} size={12} />
              <span>{t.text}</span>
              <code>{t.when}</code>
            </li>
          ))}
        </ol>
      </Feature>

      <Feature title="Search over MCP" text="Agents send a few key words and get back ranked records with a snippet and a link to the source.">
        <pre className={styles.json}>
          <code>
            {"{ "}
            <span>&quot;question&quot;</span>: &quot;saved cards&quot;, <span>&quot;k&quot;</span>: 8 {"}"}
            {"\n→ "}
            {"{ "}
            <span>&quot;source&quot;</span>: &quot;{hit.source}&quot;, <span>&quot;kind&quot;</span>: &quot;{hit.kind}&quot;,{"\n    "}
            <span>&quot;object_id&quot;</span>: &quot;{hit.object_id}&quot;,{"\n    "}
            <span>&quot;title&quot;</span>: &quot;{hit.title.replace(/\*\*/g, "")}&quot; {"}"}
          </code>
        </pre>
      </Feature>

      <Feature title="One person, every account" text="GitHub logins, Jira account ids and Slack users resolve to the same person in the graph.">
        <div className={styles.person}>
          <div className={styles.personHead}>
            <Avatar name={data.person.name} />
            <span>
              <b>{data.person.name}</b>
              <span>{data.person.title}</span>
            </span>
          </div>
          <ul>
            {data.person.accounts.map((a) => (
              <li key={a.source}>
                <SourceLogo source={a.source} size={12} />
                <code>{a.handle}</code>
              </li>
            ))}
          </ul>
        </div>
      </Feature>

      <Feature title="Two read-only tools" text="search finds records, get_record reads one in full. Neither can change anything in your tools.">
        <ul className={styles.tools}>
          <li>
            <code>search</code>
            <code className={styles.toolArgs}>question, k, sources</code>
          </li>
          <li>
            <code>get_record</code>
            <code className={styles.toolArgs}>source, object_id</code>
          </li>
          <li className={styles.toolNote}>
            <code>readOnlyHint: true</code>
          </li>
        </ul>
      </Feature>

      <Feature title="A subgraph per source" text="Each tool's records live in their own subgraph per tenant, with people and cross-source links between them.">
        <ul className={styles.subgraphs}>
          {data.counts.subgraphs.map((g) => (
            <li key={g}>
              <SourceLogo source={g.split("_").pop() as SourceId} size={12} />
              <code>{g}</code>
            </li>
          ))}
        </ul>
      </Feature>
    </div>
  );
}

/** How data moves, from docs/ingestion/architecture.md, ADR 0001 and ADR 0002, and apps/mcp. */
export function Architecture() {
  const sources: SourceId[] = ["github", "gitlab", "jira", "slack", "teams"];
  const agents = [
    { name: "Claude Code", Logo: ClaudeLogo },
    { name: "Codex", Logo: CodexLogo },
    { name: "Cursor", Logo: CursorLogo },
  ];
  return (
    <figure className={styles.arch}>
      <div className={styles.archCol}>
        <span className={styles.archLabel}>Sources</span>
        {sources.map((s) => (
          <span key={s} className={styles.archNode} data-planned={s === "teams" || undefined}>
            <SourceLogo source={s} size={14} />
            <code>{s === "teams" ? "teams · planned" : s}</code>
          </span>
        ))}
      </div>
      <div className={styles.archFlow}>
        <div className={styles.archStage}>
          <b>intake</b>
          <code>/webhook/{"{source}"}</code>
          <span>verify, store, answer 200</span>
        </div>
        <div className={styles.archStage}>
          <b>enrich</b>
          <code>raw_events → source_records</code>
          <span>one worker per source</span>
        </div>
        <div className={styles.archStage}>
          <b>extract</b>
          <code>source_records → episodes</code>
          <span>ontology mapping</span>
        </div>
        <div className={styles.archStage}>
          <b>load</b>
          <code>episodes → Neo4j</code>
          <span>Graphiti, a subgraph per source</span>
        </div>
        <div className={styles.archStore}>
          <span>
            <b>Postgres</b> <code>raw_events · source_records · episodes</code>
          </span>
          <span>
            <b>Embeddings</b> <code>Qwen3-Embedding-0.6B</code>
          </span>
        </div>
      </div>
      <div className={styles.archCol}>
        <span className={styles.archLabel}>MCP server</span>
        <span className={styles.archNode} data-hot="true">
          <EmberMark size={14} />
          <code>search · get_record</code>
        </span>
        <span className={styles.archLabel}>Agents</span>
        {agents.map(({ name, Logo }) => (
          <span key={name} className={styles.archNode}>
            <Logo size={14} />
            <span>{name}</span>
          </span>
        ))}
      </div>
      <figcaption className={styles.archNote}>
        Teams is designed (<code>docs/ingestion/teams.md</code>), not built. The MCP server is a prototype that
        searches <code>source_records</code> with full-text search; vector search over the embeddings comes next.
      </figcaption>
    </figure>
  );
}

const COMPARE = [
  ["Context lives in AGENTS.md files someone updates by hand.", "The graph updates from GitHub, Jira and Slack activity."],
  ["Your agent guesses why the code is the way it is.", "Your agent searches the decisions, tickets and threads behind it."],
  ["A ticket still says In Development after its PR merged.", "Ember flags the drift, with the evidence and a one-click fix."],
  ["One teammate is three accounts in three tools.", "People are matched across GitHub, Jira and Slack."],
];

export function BeforeAfter() {
  return (
    <table className={styles.compare}>
      <thead>
        <tr>
          <th scope="col">Without Ember</th>
          <th scope="col">With Ember</th>
        </tr>
      </thead>
      <tbody>
        {COMPARE.map(([before, after]) => (
          <tr key={before}>
            <td>{before}</td>
            <td>{after}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const FAQ = [
  [
    "What is Ember?",
    "A knowledge graph of your project's decisions, conventions, people, tickets and code, built from GitHub, Jira and Slack activity and served to AI coding tools over MCP. It replaces static context files like AGENTS.md.",
  ],
  [
    "Which tools does it read?",
    "GitHub, GitLab, Jira and Slack. Microsoft Teams is designed but not built yet. In Slack, Ember reads only the channels you invite it to.",
  ],
  [
    "Which agents can use it?",
    "Any MCP client. The server's README covers Claude Code and Codex; Cursor uses the same command in its MCP config. The server is a prototype today.",
  ],
  [
    "Can it change anything in my tools?",
    "The MCP tools are read-only. On the dashboard, fixes you apply to the preview workspace are saved in your browser only.",
  ],
  [
    "Can I try it without connecting anything?",
    "Yes. Sign up and pick GitHub, Jira or Slack to explore Kestrel Labs, a made-up team, on the dashboard and the graph.",
  ],
];

export function Faq() {
  return (
    <div className={styles.faq}>
      {FAQ.map(([q, a]) => (
        <details key={q}>
          <summary>{q}</summary>
          <p>{a}</p>
        </details>
      ))}
    </div>
  );
}
