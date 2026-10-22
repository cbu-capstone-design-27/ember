"use client";

// The fire along the bottom of the log-in and sign-up panel, after the one on
// ember-ai.dev: a low-res "Doom fire" where each cell copies the cell below it,
// drifting in the wind and cooling at random. It has more rows and color levels
// than the site's (64 and 32, against 40 and 20), so the pixels are smaller and
// the shading smoother, but it is still drawn pixelated on purpose.

import { useEffect, useRef } from "react";
import styles from "./auth.module.css";

const ROWS = 64;
const LEVELS = 32;
const FRAME_MS = 50; // ~20 fps, the site's retro cadence
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
const ALPHA = Array.from({ length: LEVELS }, (_, level) => Math.round(255 * Math.min(1, level / 6)));

export function Flame() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let width = 0;
    let heat = new Uint8Array(0);
    let image: ImageData | null = null;

    function resize() {
      const { clientWidth, clientHeight } = canvas!;
      // Hidden (the panel is display: none on narrow screens): nothing to draw.
      width = clientWidth && clientHeight ? Math.max(1, Math.round((ROWS * clientWidth) / clientHeight)) : 0;
      if (!width) return;
      canvas!.width = width;
      canvas!.height = ROWS;
      image = ctx!.createImageData(width, ROWS);
      heat = new Uint8Array(width * ROWS);
      heat.fill(LEVELS - 1, (ROWS - 1) * width); // the bottom row is the fire's source
    }

    function step() {
      for (let i = width; i < width * ROWS; i++) {
        const r = Math.floor(Math.random() * 4);
        const to = i - width - r + 1;
        if (to >= 0) heat[to] = Math.max(0, heat[i] - (r & 1));
      }
    }

    function draw() {
      if (!image) return;
      for (let i = 0; i < heat.length; i++) {
        const [r, g, b] = PALETTE[heat[i]];
        const o = i * 4;
        image.data[o] = r;
        image.data[o + 1] = g;
        image.data[o + 2] = b;
        image.data[o + 3] = ALPHA[heat[i]];
      }
      ctx!.putImageData(image, 0, 0);
    }

    // Reduced motion: let the fire rise once, then draw one still frame.
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    function still() {
      if (!width) return;
      for (let i = 0; i < ROWS * 2; i++) step();
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
