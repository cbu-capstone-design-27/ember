// The Ember mark and the marks of the sources it connects to. Source marks
// are simplified, single-file SVGs used only to label the integrations.

import type { JSX } from "react";
import type { SourceId } from "../lib/sources.ts";

export function EmberMark({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className={className} aria-hidden="true">
      <defs>
        <linearGradient id="ember-flame" x1="16" y1="2" x2="16" y2="30" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#fdba74" />
          <stop offset="0.45" stopColor="#f97316" />
          <stop offset="1" stopColor="#c2410c" />
        </linearGradient>
        <linearGradient id="ember-core" x1="16" y1="14" x2="16" y2="29" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#fef3c7" />
          <stop offset="1" stopColor="#fbbf24" />
        </linearGradient>
      </defs>
      <path
        fill="url(#ember-flame)"
        d="M16 2.5c.9 3.8 3.3 6.1 5.4 8.5 2.1 2.3 4.1 5 4.1 8.7A9.5 9.5 0 0 1 16 29.3a9.5 9.5 0 0 1-9.5-9.6c0-3.4 1.6-5.8 3.5-7.8.3 2.3 1.3 3.8 3 4.5-.6-5.2 1.1-9.7 3-13.9z"
      />
      <path
        fill="url(#ember-core)"
        d="M16 28a4.8 4.8 0 0 1-4.8-4.8c0-2.5 1.8-4.3 3.3-6.1.4 1.6 1.2 2.6 2.4 3 .2-1.2.7-2.1 1.5-2.9 1.2 1.5 2.4 3.2 2.4 5.9A4.8 4.8 0 0 1 16 28z"
      />
    </svg>
  );
}

export function Wordmark({ size = 28 }: { size?: number }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: size * 0.32 }}>
      <EmberMark size={size} />
      <span style={{ fontWeight: 700, fontSize: size * 0.64, letterSpacing: "-0.03em" }}>ember</span>
    </span>
  );
}

function GitHubMark() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path
        fill="var(--src-github)"
        d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"
      />
    </svg>
  );
}

function GitLabMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path fill="#fc6d26" d="M12 21.6 21.7 14.5 19.6 4.4 16.9 10.8H7.1L4.4 4.4 2.3 14.5z" />
      <path fill="#e24329" d="M12 21.6 7.1 10.8h9.8z" />
      <path fill="#fca326" d="M2.3 14.5 4.4 4.4l2.7 6.4zM21.7 14.5 19.6 4.4l-2.7 6.4z" />
    </svg>
  );
}

function JiraMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="1.5" y="1.5" width="21" height="21" rx="5" fill="#1868db" />
      <path fill="#fff" d="M17.6 5.6h-5.9a2.7 2.7 0 0 0 2.7 2.7h1.1v1a2.7 2.7 0 0 0 2.7 2.7V6.2a.6.6 0 0 0-.6-.6z" />
      <path fill="#fff" opacity="0.85" d="M14.7 8.5H8.8a2.7 2.7 0 0 0 2.7 2.7h1.1v1.1a2.7 2.7 0 0 0 2.7 2.7V9.1a.6.6 0 0 0-.6-.6z" />
      <path fill="#fff" opacity="0.7" d="M11.8 11.5H5.9a2.7 2.7 0 0 0 2.7 2.7h1.1v1.1a2.7 2.7 0 0 0 2.7 2.7V12.1a.6.6 0 0 0-.6-.6z" />
    </svg>
  );
}

function SlackMark() {
  // Four arms of a pinwheel, one per brand color.
  const arm = (
    <>
      <rect x="8.75" y="2" width="3.25" height="8.5" rx="1.625" />
      <rect x="4.75" y="7.25" width="3.25" height="3.25" rx="1.625" />
    </>
  );
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <g fill="#36c5f0">{arm}</g>
      <g fill="#2eb67d" transform="rotate(90 12 12)">{arm}</g>
      <g fill="#ecb22e" transform="rotate(180 12 12)">{arm}</g>
      <g fill="#e01e5a" transform="rotate(270 12 12)">{arm}</g>
    </svg>
  );
}

function TeamsMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="18.4" cy="6.4" r="2.6" fill="#7b83eb" />
      <rect x="14.5" y="10" width="8" height="9" rx="2.6" fill="#7b83eb" />
      <rect x="1.5" y="5" width="15" height="15" rx="3" fill="#5b5fc7" />
      <path fill="#fff" d="M5.2 9h7.6v2H10v6.2H8V11H5.2z" />
    </svg>
  );
}

const MARKS: Record<SourceId, () => JSX.Element> = {
  github: GitHubMark,
  gitlab: GitLabMark,
  jira: JiraMark,
  slack: SlackMark,
  teams: TeamsMark,
};

export function SourceLogo({ source, size = 20, className }: { source: SourceId; size?: number; className?: string }) {
  const Mark = MARKS[source];
  return (
    <span className={className} style={{ display: "inline-grid", width: size, height: size, flex: "none" }}>
      <Mark />
    </span>
  );
}
