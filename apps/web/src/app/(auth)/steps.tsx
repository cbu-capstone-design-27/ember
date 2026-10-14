// Where the user is in sign-up: account first, then sources (onboarding).

import styles from "./auth.module.css";

export function Steps({ current }: { current: 1 | 2 }) {
  return (
    <ol className={styles.steps} aria-label="Sign-up steps">
      <li className={`${styles.step} ${current === 1 ? styles.stepActive : ""}`} aria-current={current === 1 ? "step" : undefined}>
        <span className={styles.stepNum}>{current > 1 ? "✓" : "1"}</span> Your account
      </li>
      <li className={styles.stepLine} aria-hidden="true" />
      <li className={`${styles.step} ${current === 2 ? styles.stepActive : ""}`} aria-current={current === 2 ? "step" : undefined}>
        <span className={styles.stepNum}>2</span> Your sources
      </li>
    </ol>
  );
}
