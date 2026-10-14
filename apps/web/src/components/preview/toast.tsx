"use client";

import { useEffect } from "react";
import { CheckCircleIcon, XIcon } from "../icons.tsx";
import type { ToastState } from "./store.tsx";
import styles from "./preview.module.css";

/** One message at a time, bottom center, with Undo when the change can be taken back. */
export function Toast({ toast, onDone }: { toast: ToastState | null; onDone: () => void }) {
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(onDone, 6000);
    return () => clearTimeout(timer);
  }, [toast, onDone]);

  if (!toast) return null;
  return (
    <div className={styles.toast} role="status" key={toast.id}>
      <CheckCircleIcon className={`icon ${styles.toastIcon}`} />
      <span>{toast.text}</span>
      {toast.undo && (
        <button
          type="button"
          className={styles.toastUndo}
          onClick={() => {
            toast.undo!();
            onDone();
          }}
        >
          Undo
        </button>
      )}
      <button type="button" className={styles.toastClose} onClick={onDone} aria-label="Dismiss message">
        <XIcon />
      </button>
    </div>
  );
}
