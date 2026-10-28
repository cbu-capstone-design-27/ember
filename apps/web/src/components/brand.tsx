// The Ember mark, the wordmark, and SourceLogo, which shows each source's
// real logo from components/logos.

import type { JSX } from "react";
import type { SourceId } from "../lib/sources.ts";
import { GitHubLogo, GitLabLogo, JiraLogo, SlackLogo, TeamsLogo, type LogoProps } from "./logos/index.ts";

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
      <span style={{ fontWeight: 600, fontSize: size * 0.66, letterSpacing: "-0.025em" }}>ember</span>
    </span>
  );
}

const MARKS: Record<SourceId, (props: LogoProps) => JSX.Element> = {
  github: GitHubLogo,
  gitlab: GitLabLogo,
  jira: JiraLogo,
  slack: SlackLogo,
  teams: TeamsLogo,
};

/** A source's real logo (components/logos), at a fixed square size. */
export function SourceLogo({ source, size = 20, className }: { source: SourceId; size?: number; className?: string }) {
  const Mark = MARKS[source];
  return (
    <span className={className} style={{ display: "inline-grid", width: size, height: size, flex: "none" }}>
      <Mark size={size} />
    </span>
  );
}
