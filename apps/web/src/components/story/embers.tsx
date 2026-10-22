"use client";

// Pixel sparks rising from the fire: to the top of the log-in panel, and on
// the landing from the closing band to the top of the page (`toTop`). Render
// it inside the positioned box whose bottom `fire` fraction holds <Flame />.
// The canvas draws at the fire's own grid resolution (one canvas pixel per
// fire cell, upscaled with image-rendering: pixelated), so every spark is one
// crisp cell on the fire's grid. Sparks step up a cell at a
// time with a little sideways jitter and cool from yellow through orange and
// red to dark as they climb, fading only at the very top. Runs only while on
// screen and while the tab is visible; with reduced motion it draws one frame.

import { useEffect, useRef } from "react";
import styles from "./story.module.css";

// The fire's grid: flame.tsx draws ROWS rows over its canvas height, and
// round(ROWS * width / height) columns across it.
const FIRE_ROWS = 80;
const FRAME_MS = 40; // the fire's frame rate
const DENSITY = 0.0004; // sparks per grid cell

// Cooling ramp by height climbed, 0 at the spawn row to 1 at the top: [at, r, g, b].
const RAMP = {
  dark: [
    [0, 255, 206, 84],
    [0.25, 255, 150, 34],
    [0.55, 240, 96, 16],
    [0.85, 190, 40, 8],
    [1, 130, 22, 6],
  ],
  light: [
    [0, 245, 158, 11],
    [0.25, 234, 118, 18],
    [0.55, 226, 90, 22],
    [0.85, 171, 52, 10],
    [1, 110, 28, 6],
  ],
} as const;

interface Spark {
  x: number;
  y: number; // in rows from the top; fractional, drawn floored
  speed: number; // rows per second
  from: number; // spawn row, where it starts to cool
}

function color(ramp: (typeof RAMP)[keyof typeof RAMP], t: number) {
  let k = 0;
  while (k < ramp.length - 2 && t > ramp[k + 1][0]) k++;
  const [t0, ...a] = ramp[k];
  const [t1, ...b] = ramp[k + 1];
  const f = Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));
  return a.map((v, j) => Math.round(v + (b[j] - v) * f));
}

/**
 * `fire` is the share of the box's height the fire canvas takes (its CSS
 * height). `toTop` lets sparks climb past the box to the top of the page;
 * give it a className that puts the canvas behind the page content.
 */
export function Embers({ fire, toTop = false, className }: { fire: number; toTop?: boolean; className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const box = canvas?.parentElement;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !box || !ctx) return;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const light = matchMedia("(prefers-color-scheme: light)");
    let cols = 0;
    let rows = 0;
    let sparks: Spark[] = [];
    let frame = 0;
    let last = 0;
    let visible = false;

    function spawn(anywhere = false): Spark {
      // Born in the flame tips, above the solid core.
      const from = rows - 1 - Math.round(FIRE_ROWS * (0.3 + Math.random() * 0.4));
      return {
        x: Math.floor(Math.random() * cols),
        y: anywhere ? Math.random() * from : from,
        speed: 16 + Math.random() * 18,
        from,
      };
    }

    function resize() {
      const fw = box!.clientWidth;
      const fh = box!.clientHeight * fire;
      if (!fw || !fh) return;
      cols = Math.max(1, Math.round((FIRE_ROWS * fw) / fh));
      const cell = fh / FIRE_ROWS;
      // To the box's top, or to the page's top: the box's bottom in page coordinates.
      const height = toTop ? box!.getBoundingClientRect().bottom + window.scrollY : box!.clientHeight;
      rows = Math.ceil(height / cell);
      canvas!.width = cols;
      canvas!.height = rows;
      // Bottom-aligned like the fire, so both grids share their rows.
      canvas!.style.height = `${rows * cell}px`;
      const count = Math.max(6, Math.round(cols * rows * DENSITY));
      sparks = Array.from({ length: count }, () => spawn(true));
    }

    function draw() {
      ctx!.clearRect(0, 0, cols, rows);
      const ramp = light.matches ? RAMP.light : RAMP.dark;
      for (const s of sparks) {
        const y = Math.floor(s.y);
        if (y < 0) continue;
        const t = 1 - y / s.from;
        const [r, g, b] = color(ramp, t);
        // Full strength until the top 3% of the box, then out by the edge.
        const alpha = Math.min(1, y / (rows * 0.03));
        ctx!.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
        ctx!.fillRect(s.x, y, 1, 1);
      }
    }

    function step(t: number) {
      if (t - last >= FRAME_MS) {
        const dt = Math.min(0.1, (t - last) / 1000);
        last = t;
        for (let i = 0; i < sparks.length; i++) {
          const s = sparks[i];
          const before = Math.floor(s.y);
          s.y -= s.speed * dt;
          // Each new row, a one-cell nudge sideways now and then.
          if (Math.floor(s.y) !== before && Math.random() < 0.3) s.x += Math.random() < 0.5 ? -1 : 1;
          if (s.y < 0) sparks[i] = spawn();
        }
        draw();
      }
      frame = requestAnimationFrame(step);
    }

    function start() {
      cancelAnimationFrame(frame);
      if (reduced || !visible || document.visibilityState !== "visible" || !cols) return;
      last = performance.now();
      frame = requestAnimationFrame(step);
    }

    const ro = new ResizeObserver(() => {
      resize();
      if (reduced) draw();
      else start();
    });
    ro.observe(box);
    // The page above can change height (FAQ answers open), which moves the top.
    if (toTop) ro.observe(document.body);
    const io = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      start();
    });
    io.observe(canvas);
    document.addEventListener("visibilitychange", start);

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", start);
    };
  }, [fire, toTop]);

  return <canvas ref={ref} className={`${styles.embers} ${className ?? ""}`} aria-hidden="true" />;
}
