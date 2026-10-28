// Log-in and sign-up: the fire panel on one side, the form on the other.
// Above the fire, the panel tells the story with the preview workspace's own
// records (components/story).

import Link from "next/link";
import type { ReactNode } from "react";
import { Wordmark } from "../../components/brand.tsx";
import { connectStory, driftStory } from "../../components/story/data.ts";
import { Embers } from "../../components/story/embers.tsx";
import styles from "./auth.module.css";
import { AuthStory } from "./auth-story.tsx";
import { Flame } from "./flame.tsx";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.page}>
      <section className={styles.story} aria-hidden="true">
        <Flame />
        <Embers fire={0.32} />
        <Link href="/" className={styles.storyBrand} tabIndex={-1}>
          <Wordmark size={22} />
        </Link>
        <div className={styles.stage}>
          <AuthStory drift={driftStory()} connect={connectStory()} />
        </div>
      </section>

      <section className={styles.formSide}>
        <div className={styles.topBar}>
          <Link href="/" className={styles.mobileBrand} aria-label="Ember home">
            <Wordmark size={20} />
          </Link>
        </div>
        <div className={styles.formWrap}>{children}</div>
        <p className={styles.formFoot}>
          <Link href="/">Ember</Link>
          <span>Apache-2.0</span>
        </p>
      </section>
    </div>
  );
}
