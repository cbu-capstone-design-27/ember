import Link from "next/link";
import { redirect } from "next/navigation";
import { SourceLogo, Wordmark } from "../components/brand.tsx";
import { ArrowRightIcon } from "../components/icons.tsx";
import { ThemeSwitcher } from "../components/theme.tsx";
import { getCurrentUser } from "../lib/session.ts";
import { SOURCES, type SourceId } from "../lib/sources.ts";
import styles from "./landing.module.css";

const POINTS = [
  {
    title: "Pick your sources",
    text: "GitHub, GitLab, Jira, Slack or Teams. Ember reads pull requests, tickets and conversations, and nothing else.",
  },
  {
    title: "See what's drifting",
    text: "A PR merged but the ticket still says In Progress. Someone said it's blocked and Jira doesn't know. Each one comes with its evidence and a fix.",
  },
  {
    title: "Explore the graph",
    text: "Every person, ticket, change, message and decision, linked across tools. Your AI coding agents read the same graph.",
  },
];

// The preview workspace's status-drift example (CHK-131), as each source reports it.
const EXAMPLE: Array<{ source: SourceId; where: string; says: string; quote?: boolean; when: string; odd?: boolean }> = [
  { source: "slack", where: "#checkout-dev", says: "Saved cards (CHK-131) is done! checkout-web#219 merged", quote: true, when: "4h ago" },
  { source: "github", where: "checkout-web#219", says: "Merged", when: "5h ago" },
  { source: "jira", where: "CHK-131", says: "In Development", when: "Since 6 days ago", odd: true },
];

export default async function Home() {
  const user = await getCurrentUser();
  if (user) redirect(user.sources.length ? "/dashboard" : "/onboarding");

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Wordmark size={26} />
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
          <h1 className={styles.title}>
            Slack says it&apos;s done. <em>Jira says it isn&apos;t.</em>
          </h1>
          <div className={styles.heroSide}>
            <p className={styles.lede}>
              Ember reads GitHub, Jira and Slack, links what each one says about the same work, and shows you where they
              disagree. Your AI coding agents read the same graph.
            </p>
            <div className={styles.cta}>
              <Link className="btn btn-primary btn-lg" href="/signup">
                Create an account <ArrowRightIcon />
              </Link>
              <Link className="btn btn-secondary btn-lg" href="/login">
                Log in
              </Link>
            </div>
          </div>
        </section>

        <figure className={styles.example}>
          <figcaption className={styles.exampleHead}>
            <span className="eyebrow">Status drift</span>
            <span className={styles.exampleKey}>CHK-131 Save cards for returning customers</span>
          </figcaption>
          <ol className={styles.ledger}>
            {EXAMPLE.map((row) => (
              <li key={row.source} className={styles.ledgerRow} data-odd={row.odd}>
                <span className={styles.ledgerSource}>
                  <SourceLogo source={row.source} size={16} />
                  {SOURCES.find((s) => s.id === row.source)!.name}
                </span>
                <code className={styles.ledgerWhere}>{row.where}</code>
                <span className={row.quote ? styles.ledgerQuote : styles.ledgerSays}>
                  {row.quote ? `“${row.says}”` : row.says}
                </span>
                <span className={styles.ledgerWhen}>{row.when}</span>
              </li>
            ))}
          </ol>
          <p className={styles.exampleFix}>
            <ArrowRightIcon className={`icon ${styles.fixIcon}`} />
            Move CHK-131 to Done
          </p>
        </figure>

        <section className={styles.points} aria-label="What Ember does">
          {POINTS.map(({ title, text }) => (
            <article key={title} className={styles.point}>
              <h2>{title}</h2>
              <p>{text}</p>
            </article>
          ))}
        </section>
      </main>

      <footer className={styles.footer}>
        <Wordmark size={18} />
        <ul className={styles.sources} aria-label="Sources Ember connects to">
          {SOURCES.map((s) => (
            <li key={s.id}>{s.name}</li>
          ))}
        </ul>
      </footer>
    </div>
  );
}
