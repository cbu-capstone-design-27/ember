"use client";

// The sign-up panel: GitHub, Jira and Slack link into one graph, one at a
// time, then <Links> joins each tile to the graph card.

import { Fragment, useEffect, useRef, useState } from "react";
import { EmberMark, SourceLogo } from "../brand.tsx";
import type { ConnectStory } from "./data.ts";
import { useInView, useReducedMotion } from "./in-view.tsx";
import { Links } from "./links.tsx";
import styles from "./story.module.css";

const STEP_MS = 1100;
const HOLD_MS = 5000;

export function ConnectStoryView({ story, className }: { story: ConnectStory; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref);
  const reduced = useReducedMotion();
  const linked = story.sources.filter((s) => s.status === "linked");
  // Steps: empty, one per source, the links, then the graph card (held).
  const last = linked.length + 2;
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (reduced || !inView) return;
    const timer = setTimeout(() => setStep((s) => (s >= last ? 0 : s + 1)), step >= last ? HOLD_MS : STEP_MS);
    return () => clearTimeout(timer);
  }, [step, last, inView, reduced]);

  const shown = reduced ? last : step;
  return (
    <div ref={ref} className={`${styles.connect} ${className ?? ""}`} style={{ ["--cols" as string]: linked.length }} aria-hidden="true">
      <div className={styles.row}>
        {linked.map((s, i) => (
          <div key={s.id} className={styles.slot} data-on={shown >= i + 1} data-node={s.id}>
            <div className={styles.tile}>
              <SourceLogo source={s.id} size={18} />
              <code>
                {s.detail?.split(" · ").map((part, i, all) =>
                  i < all.length - 1 ? (
                    <Fragment key={part}>
                      <span>{part} ·</span>{" "}
                    </Fragment>
                  ) : (
                    <span key={part}>{part}</span>
                  ),
                )}
              </code>
            </div>
          </div>
        ))}
      </div>
      <div className={styles.slot} data-on={shown >= last} data-node="hub">
        <div className={styles.hub}>
          <EmberMark size={22} />
          <span>
            <b>One graph</b>
            <code>
              {story.tenant} · {story.nodes} nodes · {story.edges} edges
            </code>
          </span>
        </div>
      </div>
      <Links links={linked.map((s) => ({ from: s.id, to: "hub", on: shown >= linked.length + 1 }))} />
    </div>
  );
}
