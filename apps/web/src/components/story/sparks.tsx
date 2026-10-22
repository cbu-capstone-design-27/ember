// Embers drifting up from the fire on the log-in panel. Pure CSS; the
// positions are fixed so server and client render the same markup, and
// reduced motion stops them (globals.css).

import styles from "./story.module.css";

// [left %, delay s, duration s, drift px, size px]
const SPARKS: Array<[number, number, number, number, number]> = [
  [6, 0, 9, 14, 2], [14, 3.2, 11, -10, 3], [22, 1.4, 10, 18, 2], [31, 5.5, 12, -16, 2],
  [38, 2.1, 9.5, 12, 3], [46, 6.8, 11.5, -8, 2], [53, 0.8, 10.5, 20, 2], [61, 4.1, 12.5, -14, 3],
  [68, 2.9, 9, 10, 2], [75, 7.4, 11, -18, 2], [83, 1.9, 10, 16, 3], [91, 5, 12, -12, 2],
];

export function Sparks() {
  return (
    <div className={styles.sparks} aria-hidden="true">
      {SPARKS.map(([left, delay, duration, drift, size], i) => (
        <span
          key={i}
          style={{
            left: `${left}%`,
            width: size,
            height: size,
            animationDelay: `${delay}s`,
            animationDuration: `${duration}s`,
            ["--drift" as string]: `${drift}px`,
          }}
        />
      ))}
    </div>
  );
}
