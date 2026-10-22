"use client";

// The fire along the bottom of the log-in and sign-up panel, after the one on
// ember-ai.dev: a low-res fire on a canvas, drawn pixelated on purpose. Each
// cell is the average of the three cells below it and the one below those,
// minus a little cooling, so heat rises, spreads and fades.
//
// Shaped to read as flames, not a glow:
// - the fuel along the bottom is a few hot spots that wander, swell and fade,
//   with cold gaps between them, so it burns as separate tongues; the
//   averaging narrows each one to a point as it rises
// - cooling comes from a patchy noise map that scrolls up with the fire, so
//   flames lick and fork at their tips instead of rising as smooth columns
// - it leans to the right, so the left stays low under the footer text
// - a palette rich in orange and yellow, with only the faintest levels faded,
//   so the edges stay crisp
// - more rows and color levels than the site's (64 and 32, against 40 and
//   20), so the pixels are smaller
// - a few sparks drift up and fade above the flames

import { useEffect, useRef } from "react";
import styles from "./auth.module.css";

const ROWS = 64;
const LEVELS = 32;
const FRAME_MS = 50; // ~20 fps, the site's retro cadence
const MAX_SPARKS = 8;
const MAX_HEAT = 255;
// Heat lost per row, up to this, read from the cooling map. Sets how tall the flames burn (about 40 of the 64 rows).
const COOLING = 15;
// Coal to the white-hot base; the panel is dark in both themes, so these don't follow the theme tokens.
const STOPS = [
  [11, 8, 6],
  [70, 14, 4],
  [160, 34, 6],
  [224, 74, 10],
  [255, 118, 20],
  [255, 160, 32],
  [255, 204, 70],
  [255, 236, 150],
];
const PALETTE = Array.from({ length: LEVELS }, (_, level) => {
  const t = (level / (LEVELS - 1)) * (STOPS.length - 1);
  const k = Math.min(STOPS.length - 2, Math.floor(t));
  const f = t - k;
  return STOPS[k].map((v, j) => Math.round(v + (STOPS[k + 1][j] - v) * f));
});
// Only the faintest levels fade out, so each flame keeps a crisp edge.
const ALPHA = Array.from({ length: LEVELS }, (_, level) => Math.round(255 * Math.min(1, level / 3)));

interface Spark {
  x: number;
  y: number;
  life: number; // frames left
}

/** Smooth random patches, 0..1: white noise blurred a few times, then stretched back to the full range. */
function coolingMap(width: number, rows: number) {
  let map = Float32Array.from({ length: width * rows }, () => Math.random());
  for (let pass = 0; pass < 2; pass++) {
    const next = new Float32Array(map.length);
    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < width; x++) {
        let sum = 0;
        for (let dy = -1; dy <= 1; dy++) {
          for (let dx = -1; dx <= 1; dx++) {
            // Wraps at the edges, so the map tiles as it scrolls
            sum += map[((y + dy + rows) % rows) * width + ((x + dx + width) % width)];
          }
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

/** How strong the fire is across the panel: 0.45 at the left edge, 1 at the right. */
function lean(x: number, width: number) {
  return 0.45 + 0.55 * Math.pow(x / Math.max(1, width - 1), 1.2);
}

export function Flame() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let width = 0;
    let heat = new Float32Array(0); // 0..MAX_HEAT per cell
    let cooling = new Float32Array(0);
    let scroll = 0; // how far the cooling map has moved up, in rows
    let image: ImageData | null = null;
    let sparks: Spark[] = [];
    let tick = Math.random() * 1000; // so two page loads don't burn alike

    function resize() {
      const { clientWidth, clientHeight } = canvas!;
      // Hidden (the panel is display: none on narrow screens): nothing to draw.
      width = clientWidth && clientHeight ? Math.max(1, Math.round((ROWS * clientWidth) / clientHeight)) : 0;
      if (!width) return;
      canvas!.width = width;
      canvas!.height = ROWS;
      image = ctx!.createImageData(width, ROWS);
      heat = new Float32Array(width * ROWS);
      cooling = coolingMap(width, ROWS);
      sparks = [];
    }

    /**
     * The bottom row is the fire's source. Three slow waves of different sizes
     * add up to hot spots that wander, swell and fade; below a threshold the
     * fuel is cut off, which leaves the cold gaps that separate the flames.
     */
    function feed() {
      tick += 1;
      const base = (ROWS - 1) * width;
      for (let x = 0; x < width; x++) {
        const wave =
          0.5 +
          0.24 * Math.sin(x * 0.26 + tick * 0.05) +
          0.16 * Math.sin(x * 0.57 - tick * 0.08 + 1.3) +
          0.1 * Math.sin(x * 1.13 + tick * 0.14 + 2.1) +
          (Math.random() - 0.5) * 0.1;
        const fuel = Math.min(1, Math.max(0, (wave - 0.4) / 0.45));
        heat[base + x] = MAX_HEAT * Math.sqrt(fuel) * lean(x, width);
      }
    }

    function step() {
      feed();
      scroll = (scroll + 1) % ROWS; // the map rises a row a frame, with the heat
      // Top down, so every row reads the rows below it as they were last frame.
      for (let y = 0; y < ROWS - 1; y++) {
        const below = (y + 1) * width;
        const twoBelow = Math.min(y + 2, ROWS - 1) * width;
        const mapRow = ((y + scroll) % ROWS) * width;
        for (let x = 0; x < width; x++) {
          const left = heat[below + Math.max(0, x - 1)];
          const right = heat[below + Math.min(width - 1, x + 1)];
          const average = (left + heat[below + x] + right + heat[twoBelow + x]) / 4;
          heat[y * width + x] = Math.max(0, average - cooling[mapRow + x] * COOLING);
        }
      }
      // Sparks leave the tops of the flames, mostly where the fire is strongest, and drift up.
      if (sparks.length < MAX_SPARKS && Math.random() < 0.2) {
        const x = Math.floor(width * (0.25 + 0.75 * Math.sqrt(Math.random())));
        sparks.push({ x, y: Math.floor(ROWS * (0.4 + Math.random() * 0.25)), life: 16 + Math.floor(Math.random() * 20) });
      }
      for (const spark of sparks) {
        spark.y -= 1;
        spark.x += Math.random() < 0.25 ? -1 : Math.random() < 0.33 ? 1 : 0;
        spark.life -= 1;
      }
      sparks = sparks.filter((s) => s.life > 0 && s.y >= 0 && s.x >= 0 && s.x < width);
    }

    function draw() {
      if (!image) return;
      const data = image.data;
      for (let i = 0; i < heat.length; i++) {
        const level = Math.min(LEVELS - 1, Math.floor((heat[i] / MAX_HEAT) * LEVELS));
        const [r, g, b] = PALETTE[level];
        const o = i * 4;
        data[o] = r;
        data[o + 1] = g;
        data[o + 2] = b;
        data[o + 3] = ALPHA[level];
      }
      for (const spark of sparks) {
        const o = (spark.y * width + spark.x) * 4;
        const [r, g, b] = PALETTE[LEVELS - 4];
        data[o] = r;
        data[o + 1] = g;
        data[o + 2] = b;
        data[o + 3] = Math.round(255 * Math.min(1, spark.life / 10)); // fade out as it dies
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
