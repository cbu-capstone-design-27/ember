"use client";

// The drift, step by step, from the preview workspace's CHK-131: Slack says
// it's done, the PR merged, Jira still says In Development. Ember links the
// three and raises the flag. Loops slowly while on screen; with reduced
// motion it shows the last step and stays still.

import { useEffect, useRef, useState } from "react";
import { SourceLogo } from "../brand.tsx";
import type { Drift, DriftCard } from "./data.ts";
import { useInView, useReducedMotion } from "./in-view.tsx";
import styles from "./story.module.css";

// Step durations in ms: empty, Slack, GitHub, Jira, the links, then the flag (held).
const STEPS = [800, 1300, 1300, 1400, 1500, 5200];
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
    <div ref={ref} className={`${styles.drift} ${className ?? ""}`} data-step={shown} aria-hidden="true">
      <svg className={styles.links} viewBox="0 0 440 460">
        <path className={styles.link} data-on={shown >= 4} pathLength={1} d="M44 84 C44 170 74 160 74 240" />
        <path className={styles.link} data-on={shown >= 4} pathLength={1} d="M330 212 C330 228 300 226 296 240" />
        <path className={`${styles.link} ${styles.linkHot}`} data-on={shown >= 5} pathLength={1} d="M170 340 C170 356 190 356 190 372" />
      </svg>
      <div className={styles.slot} style={{ left: 0, top: 0 }} data-on={shown >= 1}>
        <Card card={drift.cards.slack} />
      </div>
      <div className={styles.slot} style={{ left: 160, top: 108 }} data-on={shown >= 2}>
        <Card card={drift.cards.github} />
      </div>
      <div className={styles.slot} style={{ left: 30, top: 240 }} data-on={shown >= 3}>
        <Card card={drift.cards.jira} hot={shown >= 4} />
      </div>
      <div className={`${styles.slot} ${styles.flagSlot}`} style={{ left: 70, top: 372 }} data-on={shown >= 5}>
        <div className={styles.flag}>
          <p>{drift.flag}</p>
          <span>{drift.action}</span>
        </div>
      </div>
    </div>
  );
}
