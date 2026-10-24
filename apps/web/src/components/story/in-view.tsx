"use client";

// Motion on the landing and log-in pages runs only while it can be seen, and
// not at all when the user asks for reduced motion.

import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode, type RefObject } from "react";

/** True while the element is on screen (with a small margin). */
export function useInView<T extends Element>(ref: RefObject<T | null>, margin = "120px"): boolean {
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), { rootMargin: margin });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, margin]);
  return inView;
}

const QUERY = "(prefers-reduced-motion: reduce)";

function subscribe(onChange: () => void) {
  const media = matchMedia(QUERY);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

/** The user's reduced-motion setting; false while rendering on the server. */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, () => matchMedia(QUERY).matches, () => false);
}

/**
 * A wrapper whose CSS animations pause while it is off screen
 * (`data-paused` → `animation-play-state: paused` in story.module.css).
 */
export function PauseOffscreen({ className, children }: { className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref);
  return (
    <div ref={ref} className={className} data-paused={!inView}>
      {children}
    </div>
  );
}

/** Renders its children only while on screen, e.g. the fire canvas, which animates nonstop. */
export function WhenVisible({ className, children }: { className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, "200px");
  return (
    <div ref={ref} className={className}>
      {inView && children}
    </div>
  );
}
