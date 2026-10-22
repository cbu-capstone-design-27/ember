// Log-in and sign-up: the fire on one side, the form on the other.

import Link from "next/link";
import type { ReactNode } from "react";
import { Wordmark } from "../../components/brand.tsx";
import { ThemeSwitcher } from "../../components/theme.tsx";
import styles from "./auth.module.css";
import { Flame } from "./flame.tsx";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.page}>
      <section className={styles.story} aria-hidden="true">
        <Flame />
        <Link href="/" className={`${styles.storyBrand} on-dark`} tabIndex={-1}>
          <Wordmark size={22} />
        </Link>
      </section>

      <section className={styles.formSide}>
        <div className={styles.topBar}>
          <Link href="/" className={styles.mobileBrand} aria-label="Ember home">
            <Wordmark size={20} />
          </Link>
          <ThemeSwitcher />
        </div>
        <div className={styles.formWrap}>{children}</div>
      </section>
    </div>
  );
}
