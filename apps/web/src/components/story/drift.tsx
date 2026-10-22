"use client";

// The drift, step by step, from the preview workspace's CHK-131: Slack says
// it's done, the PR merged, Jira still says In Development. Ember links the
// three and raises the flag. The cards sit on a grid; straight 1px lines run
// from the bottom edge of one card to the top edge of the next. Loops slowly
// while on screen; with reduced motion it shows the last step and stays still.

import { useEffect, useRef, useState } from "react";
import { SourceLogo } from "../brand.tsx";
import type { Drift, DriftCard } from "./data.ts";
import { useInView, useReducedMotion } from "./in-view.tsx";
import styles from "./story.module.css";

// Step durations in ms: empty, Slack, GitHub, Jira, the links, then the flag (held).
const STEPS = [700, 1100, 1100, 1200, 1300, 5200];
const LAST = STEPS.length - 1;

function Card({ card, hot }: { card: DriftCard; hot?: boolean }) {
  return (
    <div className={styles.card} data-hot={hot}>
      <div className={styles.cardHead}>
        <SourceLogo source={card.source} size={14} />
        <code>{card.where}</code>
        <code className={styles.cardWhen}>{card.when}</code>
      </div>
      <p className={styles.cardText}>
        {card.who && <b>{card.who} </b>}
        {card.text}
      </p>
      {card.state && <span className={styles.cardState}>{card.state}</span>}
    </div>
  );
}

export function DriftStory({ drift, className }: { drift: Drift; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref);
  const reduced = useReducedMotion();
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (reduced || !inView) return;
    const timer = setTimeout(() => setStep((s) => (s >= LAST ? 0 : s + 1)), STEPS[step]);
    return () => clearTimeout(timer);
  }, [step, inView, reduced]);

  const shown = reduced ? LAST : step;
  return (
    <div ref={ref} className={`${styles.drift} ${className ?? ""}`} aria-hidden="true">
      <div className={styles.pair}>
        <div className={styles.slot} data-on={shown >= 1}>
          <Card card={drift.cards.slack} />
        </div>
        <div className={styles.slot} data-on={shown >= 2}>
          <Card card={drift.cards.github} />
        </div>
      </div>
      <div className={styles.pair}>
        <span className={styles.wire} data-on={shown >= 4} />
        <span className={styles.wire} data-on={shown >= 4} />
      </div>
      <div className={styles.slot} data-on={shown >= 3}>
        <Card card={drift.cards.jira} hot={shown >= 4} />
      </div>
      <div className={styles.single}>
        <span className={`${styles.wire} ${styles.wireHot}`} data-on={shown >= 5} />
      </div>
      <div className={styles.slot} data-on={shown >= 5}>
        <div className={styles.flag}>
          <p>{drift.flag}</p>
          <span>{drift.action}</span>
        </div>
      </div>
    </div>
  );
}
