import Link from "next/link";
import { redirect } from "next/navigation";
import { SourceLogo, Wordmark } from "../components/brand.tsx";
import { ArrowRightIcon, DriftIcon, GraphIcon, LayersIcon } from "../components/icons.tsx";
import { ThemeSwitcher } from "../components/theme.tsx";
import { getCurrentUser } from "../lib/session.ts";
import { SOURCES } from "../lib/sources.ts";
import styles from "./landing.module.css";

const FEATURES = [
  {
    Icon: LayersIcon,
    title: "Connect your sources",
    text: "Pick the tools your team works in. Ember reads pull requests, tickets and conversations, and nothing else.",
  },
  {
    Icon: DriftIcon,
    title: "See what's drifting",
    text: "A PR merged but the ticket says in progress. Someone said it's blocked, Jira doesn't know. Ember points it out.",
  },
  {
    Icon: GraphIcon,
    title: "Explore the graph",
    text: "Every person, ticket, change, message and decision, linked across tools. The same graph your AI agents read.",
  },
];

export default async function Home() {
  const user = await getCurrentUser();
  if (user) redirect(user.sources.length ? "/dashboard" : "/onboarding");

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Wordmark size={28} />
        <div className={styles.headerActions}>
          <ThemeSwitcher />
          <Link className="btn btn-ghost" href="/login">
            Log in
          </Link>
          <Link className="btn btn-primary" href="/signup">
            Get started
          </Link>
        </div>
      </header>

      <main className={styles.main}>
        <section className={styles.hero}>
          <span className="badge badge-accent">Preview</span>
          <h1 className={styles.title}>Your team&apos;s context, connected.</h1>
          <p className={styles.lede}>
            Ember turns GitHub, Jira and Slack activity into one knowledge graph of decisions, people, tickets and
            code, for your team and for the AI coding agents that work with it.
          </p>
          <div className={styles.cta}>
            <Link className="btn btn-primary btn-lg" href="/signup">
              Create your account <ArrowRightIcon />
            </Link>
            <Link className="btn btn-secondary btn-lg" href="/login">
              Log in
            </Link>
          </div>
          <ul className={styles.sources} aria-label="Sources Ember connects to">
            {SOURCES.map((s) => (
              <li key={s.id}>
                <SourceLogo source={s.id} size={18} />
                {s.name}
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.features}>
          {FEATURES.map(({ Icon, title, text }) => (
            <article key={title} className={`card ${styles.feature}`}>
              <span className={styles.featureIcon}>
                <Icon />
              </span>
              <h2>{title}</h2>
              <p>{text}</p>
            </article>
          ))}
        </section>
      </main>
    </div>
  );
}
