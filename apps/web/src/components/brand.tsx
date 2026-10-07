// The Ember mark and the marks of the sources it connects to. Source marks
// are simplified, single-file SVGs used only to label the integrations.

import type { JSX } from "react";
import type { SourceId } from "../lib/sources.ts";

/**
 * The Ember logo: an orange flame, a yellow core, and a small graph of five
 * nodes. app/icon.svg is the same drawing, used as the browser tab icon.
 */
export function EmberMark({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="128 187 900 900"
      className={className ? `ember-mark ${className}` : "ember-mark"}
      aria-hidden="true"
    >
      <path
        fill="#ff7420"
        d="M575 1083C400 1083 285 985 285 850C285 775 303 720 330 683C337 730 357 765 392 787C366 702 372 600 430 503C486 410 548 320 525 192C625 248 702 345 702 475C702 565 678 615 686 670C716 600 757 556 800 527C792 600 830 662 856 722C872 762 872 812 868 852C858 990 740 1083 575 1083Z"
      />
      <path
        fill="#fdb73b"
        d="M575 1080C452 1080 362 1002 362 902C362 862 365 836 372 815C392 840 412 853 444 863C414 790 412 700 462 620C504 556 572 500 583 428C640 482 652 560 652 632C654 702 680 742 712 772C716 745 722 722 730 703C774 750 795 820 790 882C784 1000 700 1080 575 1080Z"
      />
      <path fill="none" stroke="#fbf3e6" strokeWidth="13" strokeLinecap="round" d="M568 822 497 698M568 822 447 937M568 822 715 853 751 938" />
      <g fill="#fbf3e6">
        <circle cx="568" cy="822" r="45" />
        <circle cx="497" cy="698" r="27" />
        <circle cx="447" cy="937" r="29" />
        <circle cx="715" cy="853" r="24" />
        <circle cx="751" cy="938" r="20" />
      </g>
    </svg>
  );
}

export function Wordmark({ size = 28 }: { size?: number }) {
  return (
    // The flame is tall and narrow, so it's drawn a little larger than the text to read at the same weight.
    <span style={{ display: "inline-flex", alignItems: "center", gap: size * 0.2 }}>
      <EmberMark size={Math.round(size * 1.25)} />
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
