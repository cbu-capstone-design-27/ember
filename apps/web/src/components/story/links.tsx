"use client";

// Connectors between cards: rounded orthogonal paths, a dot at each end, a
// gradient stroke that draws in when the link turns on. <Links> measures the
// cards marked data-node="<id>" inside its positioned parent and joins the
// bottom-edge midpoint of `from` to the top-edge midpoint of `to`.

import { useLayoutEffect, useRef, useState } from "react";
import { elbow } from "./elbow.ts";
import styles from "./story.module.css";

export interface Link {
  from: string;
  to: string;
  on: boolean;
  hot?: boolean;
}

interface Drawn extends Link {
  d: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

// Centres a 1.5px stroke on a device pixel boundary so it renders crisp.
function snap(v: number) {
  const dpr = window.devicePixelRatio || 1;
  return (Math.floor(v * dpr) + 0.5) / dpr;
}

export function Links({ links }: { links: Link[] }) {
  const ref = useRef<SVGSVGElement>(null);
  const [drawn, setDrawn] = useState<Drawn[]>([]);
  const key = links.map((l) => `${l.from}>${l.to}`).join(",");

  useLayoutEffect(() => {
    const box = ref.current?.parentElement;
    if (!box) return;
    function measure() {
      const node = (id: string) => box!.querySelector<HTMLElement>(`[data-node="${id}"]`);
      const next: Drawn[] = [];
      for (const link of links) {
        const a = node(link.from);
        const b = node(link.to);
        if (!a || !b) continue;
        // offset* ignore the slots' entry transform, so the lines never move.
        const x1 = snap(a.offsetLeft + a.offsetWidth / 2);
        const y1 = Math.round(a.offsetTop + a.offsetHeight);
        const x2 = snap(b.offsetLeft + b.offsetWidth / 2);
        const y2 = Math.round(b.offsetTop);
        next.push({ ...link, d: elbow(x1, y1, x2, y2), x1, y1, x2, y2 });
      }
      setDrawn(next);
    }
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(box);
    return () => ro.disconnect();
    // Re-measure when the set of links changes, not when they turn on or off.
  }, [key]);

  const on = new Map(links.map((l) => [`${l.from}>${l.to}`, l]));
  return (
    <svg ref={ref} className={styles.links} aria-hidden="true">
      {drawn.map((l, i) => {
        const state = on.get(`${l.from}>${l.to}`) ?? l;
        const id = `link-${l.from}-${l.to}`;
        return (
          <g key={id} className={styles.link} data-on={state.on} data-hot={state.hot}>
            <defs>
              <linearGradient id={id} gradientUnits="userSpaceOnUse" x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2}>
                <stop offset="0" className={styles.linkFrom} />
                <stop offset="1" className={styles.linkTo} />
              </linearGradient>
            </defs>
            <path d={l.d} pathLength={1} stroke={`url(#${id})`} style={{ transitionDelay: `${i * 80}ms` }} />
            <circle cx={l.x1} cy={l.y1} r={2.5} className={styles.linkFromDot} />
            <circle cx={l.x2} cy={l.y2} r={2.5} className={styles.linkToDot} />
          </g>
        );
      })}
    </svg>
  );
}
