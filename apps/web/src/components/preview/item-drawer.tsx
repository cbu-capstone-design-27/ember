"use client";

// The side sheet that opens when anything on the dashboard is clicked. It
// keeps a stack, so following links between items can be walked back.

import { useEffect, useRef } from "react";
import { ArrowLeftIcon, XIcon } from "../icons.tsx";
import { ItemDetails, ItemList } from "./item-details.tsx";
import { usePreview, type DrawerView } from "./store.tsx";
import styles from "./preview.module.css";

export function ItemDrawer({ stack, onBack, onClose }: { stack: DrawerView[]; onBack: () => void; onClose: () => void }) {
  const { openItem } = usePreview();
  const panel = useRef<HTMLDivElement>(null);
  const view = stack[stack.length - 1];

  useEffect(() => {
    if (!view) return;
    panel.current?.focus();
    panel.current?.scrollTo({ top: 0 });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, onClose]);

  if (!view) return null;
  return (
    <div className={styles.drawerRoot}>
      <div className={styles.backdrop} onClick={onClose} aria-hidden="true" />
      <div ref={panel} className={styles.drawer} role="dialog" aria-modal="true" aria-label={view.kind === "list" ? view.title : "Item details"} tabIndex={-1}>
        <div className={styles.drawerBar}>
          {stack.length > 1 && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}>
              <ArrowLeftIcon /> Back
            </button>
          )}
          {view.kind === "list" && <h2 className={styles.drawerTitle}>{view.title}</h2>}
          <button type="button" className="btn btn-ghost btn-icon btn-sm" onClick={onClose} aria-label="Close" style={{ marginLeft: "auto" }}>
            <XIcon />
          </button>
        </div>
        <div className={styles.drawerBody}>
          {view.kind === "item" ? <ItemDetails key={view.id} id={view.id} onOpen={openItem} /> : <ItemList ids={view.ids} onOpen={openItem} />}
        </div>
      </div>
    </div>
  );
}
