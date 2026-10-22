// Embers rising from the fire to the top of their box (the log-in panel and
// the landing's closing band). Pure CSS. The positions are fixed so server
// and client render the same markup, and reduced motion stops them (globals.css).

import styles from "./story.module.css";

// [left %, delay s, duration s, drift px, size px]
const SPARKS: Array<[number, number, number, number, number]> = [
  [6, 0, 11, 24, 2], [14, 3.2, 13, -18, 3], [22, 1.4, 12, 30, 2], [31, 5.5, 14, -26, 2],
  [38, 2.1, 11.5, 20, 3], [46, 6.8, 13.5, -14, 2], [53, 0.8, 12.5, 32, 2], [61, 4.1, 14.5, -22, 3],
  [68, 2.9, 11, 16, 2], [75, 7.4, 13, -30, 2], [83, 1.9, 12, 26, 3], [91, 5, 14, -20, 2],
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
