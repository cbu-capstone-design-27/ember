"use client";

// The grid of sources a team can connect. Used in onboarding (step 2 of
// sign-up) and in Settings; both save to user.sources.

import { useRouter } from "next/navigation";
import { useState } from "react";
import { authClient } from "../lib/auth-client.ts";
import { authErrorMessage } from "../lib/auth-errors.ts";
import { SOURCES, SOURCE_IDS, type SourceId } from "../lib/sources.ts";
import { SourceLogo } from "./brand.tsx";
import { AlertIcon, ArrowRightIcon, CheckIcon } from "./icons.tsx";
import styles from "./source-picker.module.css";

export function SourceGrid({ value, onChange }: { value: SourceId[]; onChange: (next: SourceId[]) => void }) {
  const toggle = (id: SourceId) =>
    onChange(value.includes(id) ? value.filter((v) => v !== id) : SOURCE_IDS.filter((s) => s === id || value.includes(s)));

  return (
    <div className={styles.grid} role="group" aria-label="Sources">
      {SOURCES.map((s) => {
        const checked = value.includes(s.id);
        return (
          <label key={s.id} className={styles.card} data-checked={checked} data-source={s.id}>
            <input
              type="checkbox"
              className="visually-hidden"
              checked={checked}
              onChange={() => toggle(s.id)}
              aria-describedby={`source-${s.id}-desc`}
            />
            <span className={styles.top}>
              <span className={styles.logo}>
                <SourceLogo source={s.id} size={26} />
              </span>
              <span className={styles.check} aria-hidden="true">
                <CheckIcon />
              </span>
            </span>
            <span className={styles.name}>{s.name}</span>
            <span className={styles.category}>{s.category}</span>
            <span id={`source-${s.id}-desc`} className={styles.description}>
              {s.description}
            </span>
            <ul className={styles.reads}>
              {s.reads.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
            <span className={styles.footer}>
              {s.preview ? (
                <span className="badge badge-accent">Preview data ready</span>
              ) : (
                <span className="badge">Connects later</span>
              )}
            </span>
          </label>
        );
      })}
    </div>
  );
}

export function SourcesForm({ initial, mode }: { initial: SourceId[]; mode: "onboarding" | "settings" }) {
  const router = useRouter();
  const [value, setValue] = useState<SourceId[]>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const dirty = value.join() !== initial.join();
  const all = value.length === SOURCE_IDS.length;

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    const { error } = await authClient.updateUser({ sources: value });
    setBusy(false);
    if (error) {
      setError(authErrorMessage(error, "Couldn't save your sources. Try again."));
      return;
    }
    if (mode === "onboarding") {
      router.replace("/dashboard");
    } else {
      setSaved(true);
    }
    router.refresh();
  }

  return (
    <div className={styles.form}>
      <SourceGrid
        value={value}
        onChange={(next) => {
          setValue(next);
          setSaved(false);
        }}
      />
      {error && (
        <p className="alert alert-error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </p>
      )}
      <div className={styles.actions}>
        <p className={styles.count} aria-live="polite">
          {value.length === 0 ? "Pick at least one source." : `${value.length} of ${SOURCE_IDS.length} selected`}
          <button type="button" className={styles.toggleAll} onClick={() => setValue(all ? [] : [...SOURCE_IDS])}>
            {all ? "Clear" : "Select all"}
          </button>
        </p>
        {saved && !dirty && <span className="badge badge-success">Saved</span>}
        <button
          type="button"
          className={`btn btn-primary ${mode === "onboarding" ? "btn-lg" : ""}`}
          disabled={busy || value.length === 0 || (mode === "settings" && !dirty)}
          onClick={save}
        >
          {busy && <span className="spinner" aria-hidden="true" />}
          {mode === "onboarding" ? (
            <>
              Continue to your dashboard <ArrowRightIcon />
            </>
          ) : (
            "Save sources"
          )}
        </button>
      </div>
    </div>
  );
}
