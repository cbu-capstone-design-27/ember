"use client";

// The fire along the bottom of the log-in and sign-up panel, after the one on
// ember-ai.dev: a low-res "Doom fire" where each cell copies the cell below it,
// drifting in the wind and cooling at random, drawn pixelated on purpose.
//
// Kept quiet so it frames the panel instead of competing with it:
// - more rows and color levels than the site's (64 and 32, against 40 and 20),
//   so the pixels are smaller and the shading smoother
// - the fuel along the bottom wanders slowly and leans to the right, so it
//   burns as tongues of flame, not a wall, and the left stays low embers
// - a few sparks drift up and fade above the flames
// - the CSS adds a soft glow behind it (auth.module.css, .flame)

import { useEffect, useRef } from "react";
import styles from "./auth.module.css";

const ROWS = 64;
const LEVELS = 32;
const FRAME_MS = 50; // ~20 fps, the site's retro cadence
const MAX_SPARKS = 7;
// Coal to flame tip; the panel is dark in both themes, so these don't follow the theme tokens.
const STOPS = [
  [11, 8, 6],
  [42, 10, 4],
  [110, 20, 5],
  [184, 50, 10],
  [255, 106, 26],
  [255, 168, 58],
];
const PALETTE = Array.from({ length: LEVELS }, (_, level) => {
  const t = (level / (LEVELS - 1)) * (STOPS.length - 1);
  const k = Math.min(STOPS.length - 2, Math.floor(t));
  const f = t - k;
  return STOPS[k].map((v, j) => Math.round(v + (STOPS[k + 1][j] - v) * f));
});
// The coolest levels fade out, so the fire blends into the panel instead of sitting in a dark box.
const ALPHA = Array.from({ length: LEVELS }, (_, level) => Math.round(255 * Math.min(1, level / 7)));

interface Spark {
  x: number;
  y: number;
  life: number; // frames left
}

/** How much fuel each column gets, from 0.4 at the left edge to 1 at the right. */
function lean(x: number, width: number) {
  return 0.4 + 0.6 * Math.pow(x / Math.max(1, width - 1), 1.4);
}

export function Flame() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let width = 0;
    let heat = new Uint8Array(0);
    let fuel = new Float32Array(0);
    let image: ImageData | null = null;
    let sparks: Spark[] = [];

    function resize() {
      const { clientWidth, clientHeight } = canvas!;
      // Hidden (the panel is display: none on narrow screens): nothing to draw.
      width = clientWidth && clientHeight ? Math.max(1, Math.round((ROWS * clientWidth) / clientHeight)) : 0;
      if (!width) return;
      canvas!.width = width;
      canvas!.height = ROWS;
      image = ctx!.createImageData(width, ROWS);
      heat = new Uint8Array(width * ROWS);
      fuel = Float32Array.from({ length: width }, () => 0.6 + Math.random() * 0.4);
      sparks = [];
    }

    /** The bottom row is the fire's source: fuel wanders a little each frame, smoothed with its neighbours. */
    function feed() {
      for (let x = 0; x < width; x++) {
        fuel[x] = Math.min(1, Math.max(0.35, fuel[x] + (Math.random() - 0.5) * 0.12));
      }
      for (let x = 1; x < width - 1; x++) fuel[x] = (fuel[x - 1] + 2 * fuel[x] + fuel[x + 1]) / 4;
      const base = (ROWS - 1) * width;
      for (let x = 0; x < width; x++) heat[base + x] = Math.round((LEVELS - 1) * fuel[x] * lean(x, width));
    }

    function step() {
      feed();
      for (let i = width; i < width * ROWS; i++) {
        const r = Math.floor(Math.random() * 4);
        const to = i - width - r + 1;
        if (to >= 0) heat[to] = Math.max(0, heat[i] - (r & 1));
      }
      // Sparks leave the flames' upper edge, mostly where the fire is strongest, and drift up.
      if (sparks.length < MAX_SPARKS && Math.random() < 0.18) {
        const x = Math.floor(width * (0.3 + 0.7 * Math.sqrt(Math.random())));
        sparks.push({ x, y: Math.floor(ROWS * (0.45 + Math.random() * 0.2)), life: 18 + Math.floor(Math.random() * 18) });
      }
      for (const spark of sparks) {
        spark.y -= 1;
        spark.x += Math.random() < 0.5 ? -1 : Math.random() < 0.3 ? 1 : 0; // the same leftward wind as the fire
        spark.life -= 1;
      }
      sparks = sparks.filter((s) => s.life > 0 && s.y >= 0 && s.x >= 0 && s.x < width);
    }

    function draw() {
      if (!image) return;
      const data = image.data;
      for (let i = 0; i < heat.length; i++) {
        const [r, g, b] = PALETTE[heat[i]];
        const o = i * 4;
        data[o] = r;
        data[o + 1] = g;
        data[o + 2] = b;
        data[o + 3] = ALPHA[heat[i]];
      }
      for (const spark of sparks) {
        const o = (spark.y * width + spark.x) * 4;
        const [r, g, b] = PALETTE[LEVELS - 1];
        data[o] = r;
        data[o + 1] = g;
        data[o + 2] = b;
        data[o + 3] = Math.round(255 * Math.min(1, spark.life / 12)); // fade out as it dies
      }
      ctx!.putImageData(image, 0, 0);
    }

    // Reduced motion: let the fire rise once, then draw one still frame (no sparks).
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    function still() {
      if (!width) return;
      for (let i = 0; i < ROWS * 2; i++) step();
      sparks = [];
      draw();
    }

    resize();
    const observer = new ResizeObserver(() => {
      resize();
      if (reduced) still();
    });
    observer.observe(canvas);

    let frame = 0;
    if (reduced) {
      still();
    } else {
      let last = 0;
      const loop = (t: number) => {
        if (width && t - last > FRAME_MS) {
          step();
          draw();
          last = t;
        }
        frame = requestAnimationFrame(loop);
      };
      frame = requestAnimationFrame(loop);
    }

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);

  return <canvas ref={canvasRef} className={styles.flame} aria-hidden="true" />;
}
