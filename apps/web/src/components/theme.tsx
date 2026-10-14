"use client";

// Light, dark, or follow the system. The choice is kept in localStorage (a
// per-browser convenience) and applied to <html data-theme> before first
// paint by THEME_SCRIPT (theme-script.ts).

import { useSyncExternalStore } from "react";
import { MonitorIcon, MoonIcon, SunIcon } from "./icons.tsx";
import { THEME_KEY as KEY } from "./theme-script.ts";

export type ThemeChoice = "light" | "dark" | "system";

const EVENT = "ember-theme-change";

function readChoice(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function apply(choice: ThemeChoice) {
  const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  const root = document.documentElement;
  root.dataset.theme = dark ? "dark" : "light";
  root.style.colorScheme = dark ? "dark" : "light";
}

export function setTheme(choice: ThemeChoice) {
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    // Storage can be blocked; the theme still applies for this page view.
  }
  apply(choice);
  window.dispatchEvent(new Event(EVENT));
}

function subscribe(onChange: () => void) {
  const media = matchMedia("(prefers-color-scheme: dark)");
  const onSystem = () => {
    if (readChoice() === "system") apply("system");
    onChange();
  };
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY) {
      apply(readChoice());
      onChange();
    }
  };
  media.addEventListener("change", onSystem);
  window.addEventListener("storage", onStorage);
  window.addEventListener(EVENT, onChange);
  return () => {
    media.removeEventListener("change", onSystem);
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(EVENT, onChange);
  };
}

/** The saved choice; null while rendering on the server. */
export function useThemeChoice(): ThemeChoice | null {
  return useSyncExternalStore(subscribe, readChoice, () => null);
}

const OPTIONS: Array<{ value: ThemeChoice; label: string; Icon: typeof SunIcon }> = [
  { value: "light", label: "Light", Icon: SunIcon },
  { value: "dark", label: "Dark", Icon: MoonIcon },
  { value: "system", label: "System", Icon: MonitorIcon },
];

export function ThemeSwitcher({ showLabels = false }: { showLabels?: boolean }) {
  const choice = useThemeChoice();
  return (
    <div className="segmented" role="group" aria-label="Theme">
      {OPTIONS.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          aria-pressed={choice === value}
          title={showLabels ? undefined : label}
          aria-label={showLabels ? undefined : label}
          onClick={() => setTheme(value)}
        >
          <Icon />
          {showLabels && <span>{label}</span>}
        </button>
      ))}
    </div>
  );
}
