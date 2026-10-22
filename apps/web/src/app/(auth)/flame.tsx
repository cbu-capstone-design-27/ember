"use client";

// The fire along the bottom of the log-in and sign-up panel, after the one on
// ember-ai.dev: a low-res fire on a canvas, drawn pixelated on purpose.
//
// One fire across the whole width, with flame shapes in it:
// - every column burns. Fuel is a steady bed plus a few hot spots that drift
//   slowly across the panel and swell and fade, so the body stays continuous
//   and the tallest flames move around
// - each cell takes the heat of the cells below it, minus cooling read from a
//   patchy noise map that scrolls up with the fire. Cool patches cut the heat
//   into tongues that rise, narrow and break off at their tips. The map is
//   fixed, not re-rolled per frame, so the motion is smooth and doesn't flicker
// - wind is two slow waves that bend the flames left and right over time
// - colors run deep red, orange, amber, yellow, to a pale core at the base

import { useEffect, useRef } from "react";
import styles from "./auth.module.css";

const ROWS = 80;
const FRAME_MS = 40; // ~25 fps
const SPOTS = 4; // drifting hot spots
// Heat lost per row at the coolest patch of the noise map (heat runs 0..1). Sets the flame height.
const COOLING = 0.13;
// Cool to hottest; the panel is dark in both themes, so these don't follow the theme tokens.
// Each stop is [heat, r, g, b, alpha]: the coolest heat fades out into the panel.
const STOPS = [
  [0.0, 60, 8, 2, 0],
  [0.12, 110, 16, 4, 140],
  [0.28, 190, 40, 8, 235],
  [0.45, 240, 96, 16, 255],
  [0.62, 255, 150, 34, 255],
  [0.8, 255, 206, 84, 255],
  [1.0, 255, 244, 200, 255],
];
const LEVELS = 256;
// Heat 0..1 to RGBA, one entry per level.
const PALETTE = Array.from({ length: LEVELS }, (_, level) => {
  const h = level / (LEVELS - 1);
  let k = 0;
  while (k < STOPS.length - 2 && h > STOPS[k + 1][0]) k++;
  const [h0, ...a] = STOPS[k];
  const [h1, ...b] = STOPS[k + 1];
  const f = Math.min(1, Math.max(0, (h - h0) / (h1 - h0)));
  return a.map((v, j) => Math.round(v + (b[j] - v) * f));
});

/** Smooth random patches, 0..1: white noise blurred a few times, stretched back to the full range. Tiles at the edges. */
function noiseMap(width: number, rows: number) {
  let map = Float32Array.from({ length: width * rows }, () => Math.random());
  for (let pass = 0; pass < 3; pass++) {
    const next = new Float32Array(map.length);
    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < width; x++) {
        let sum = 0;
        for (let dy = -1; dy <= 1; dy++) {
          for (let dx = -1; dx <= 1; dx++) sum += map[((y + dy + rows) % rows) * width + ((x + dx + width) % width)];
        }
        next[y * width + x] = sum / 9;
      }
    }
    map = next;
  }
  let low = 1;
  let high = 0;
  for (const v of map) (low = Math.min(low, v)), (high = Math.max(high, v));
  return map.map((v) => (v - low) / Math.max(1e-6, high - low));
}

/** A row's heat at fractional column x, softened with its neighbours. */
function lerp(row: Float32Array, x: number) {
  const n = row.length;
  const at = (c: number) => row[Math.min(n - 1, Math.max(0, c))];
  const c = Math.floor(x);
  const f = x - c;
  const blur = (c: number) => 0.25 * at(c - 1) + 0.5 * at(c) + 0.25 * at(c + 1);
  return blur(c) * (1 - f) + blur(c + 1) * f;
}

export function Flame() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let width = 0;
    let heat = new Float32Array(0);
    let next = new Float32Array(0);
    let noise = new Float32Array(0);
    let image: ImageData | null = null;
    let tick = Math.floor(Math.random() * 1000); // so two page loads don't burn alike
    // Each hot spot drifts at its own speed and swells on its own beat.
    const spots = Array.from({ length: SPOTS }, (_, i) => ({
      x: (i + Math.random()) / SPOTS,
      speed: (Math.random() < 0.5 ? -1 : 1) * (0.0006 + Math.random() * 0.0008),
      phase: Math.random() * Math.PI * 2,
    }));

    function resize() {
      const { clientWidth, clientHeight } = canvas!;
      // Hidden (the panel is display: none on narrow screens): nothing to draw.
      width = clientWidth && clientHeight ? Math.max(1, Math.round((ROWS * clientWidth) / clientHeight)) : 0;
      if (!width) return;
      canvas!.width = width;
      canvas!.height = ROWS;
      image = ctx!.createImageData(width, ROWS);
      heat = new Float32Array(width * ROWS);
      next = new Float32Array(width * ROWS);
      noise = noiseMap(width, ROWS);
    }

    /** The bottom two rows are the fuel: a steady bed everywhere, hotter under the drifting spots. */
    function feed() {
      for (const s of spots) s.x = (s.x + s.speed + 1) % 1;
      for (let x = 0; x < width; x++) {
        const u = x / width;
        let fuel = 0.56 + 0.05 * Math.sin(u * 23 + tick * 0.031);
        for (const s of spots) {
          const d = Math.min(Math.abs(u - s.x), 1 - Math.abs(u - s.x)); // wraps, so spots cross the edges smoothly
          fuel += (0.32 + 0.12 * Math.sin(tick * 0.02 + s.phase)) * Math.exp(-(d * d) / 0.006);
        }
        heat[(ROWS - 1) * width + x] = heat[(ROWS - 2) * width + x] = Math.min(1, fuel);
      }
    }

    function step() {
      tick += 1;
      feed();
      // Wind, in cells per row: a slow sway plus a slower drift.
      const wind = 0.22 * Math.sin(tick * 0.011) + 0.12 * Math.sin(tick * 0.0043 + 1.7);
      const scroll = tick % ROWS; // the noise map moves up one row per frame, with the flames
      for (let y = 0; y < ROWS - 2; y++) {
        const ny = ((y + scroll) % ROWS) * width;
        const row = heat.subarray((y + 1) * width, (y + 2) * width);
        const two = heat.subarray((y + 2) * width, (y + 3) * width);
        for (let x = 0; x < width; x++) {
          // Heat from the two rows below, each shifted by the wind.
          const h = 0.55 * lerp(row, x - wind) + 0.45 * lerp(two, x - 2 * wind);
          const n = noise[ny + x];
          next[y * width + x] = Math.max(0, h - COOLING * n * n);
        }
      }
      for (let i = 0; i < (ROWS - 2) * width; i++) heat[i] = next[i];
    }

    function draw() {
      if (!image) return;
      const data = image.data;
      for (let i = 0; i < heat.length; i++) {
        const c = PALETTE[Math.round(heat[i] * (LEVELS - 1))];
        const o = i * 4;
        data[o] = c[0];
        data[o + 1] = c[1];
        data[o + 2] = c[2];
        data[o + 3] = c[3];
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
    let last = 0;
    const loop = (t: number) => {
      if (width && t - last > FRAME_MS) {
        step();
        draw();
        last = t;
      }
      frame = requestAnimationFrame(loop);
    };
    // Paused while the tab is hidden.
    const onVisibility = () => {
      cancelAnimationFrame(frame);
      if (document.visibilityState === "visible") frame = requestAnimationFrame(loop);
    };
    if (reduced) {
      still();
    } else {
      onVisibility();
      document.addEventListener("visibilitychange", onVisibility);
    }

    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("visibilitychange", onVisibility);
      observer.disconnect();
    };
  }, []);

  return <canvas ref={canvasRef} className={styles.flame} aria-hidden="true" />;
}
