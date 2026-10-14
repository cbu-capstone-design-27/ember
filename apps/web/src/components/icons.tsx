// Line icons, 24×24, drawn with currentColor. Inline so the app needs no icon
// font or network request (it also runs offline inside the desktop shell).

import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { title?: string };

function icon(paths: ReactNode) {
  return function Icon({ title, className = "icon", ...props }: IconProps) {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        aria-hidden={title ? undefined : true}
        role={title ? "img" : undefined}
        {...props}
      >
        {title && <title>{title}</title>}
        {paths}
      </svg>
    );
  };
}

export const DashboardIcon = icon(
  <>
    <rect x="3" y="3" width="7.5" height="9" rx="1.6" />
    <rect x="13.5" y="3" width="7.5" height="5" rx="1.6" />
    <rect x="13.5" y="11" width="7.5" height="10" rx="1.6" />
    <rect x="3" y="15" width="7.5" height="6" rx="1.6" />
  </>,
);

export const GraphIcon = icon(
  <>
    <circle cx="5.5" cy="6" r="2.5" />
    <circle cx="18.5" cy="7.5" r="2.5" />
    <circle cx="11" cy="18.5" r="2.5" />
    <path d="M8 6.4 16 7.1M6.7 8.3l3.2 7.9M16.9 9.5l-4.4 7" />
  </>,
);

export const SettingsIcon = icon(
  <>
    <path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" />
    <circle cx="15" cy="6" r="2" />
    <circle cx="9" cy="12" r="2" />
    <circle cx="17" cy="18" r="2" />
  </>,
);

export const LogOutIcon = icon(
  <>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <path d="m16 17 5-5-5-5M21 12H9" />
  </>,
);

export const SunIcon = icon(
  <>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4" />
  </>,
);

export const MoonIcon = icon(<path d="M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.6 6.6 0 0 0 10 10z" />);

export const MonitorIcon = icon(
  <>
    <rect x="2.5" y="3.5" width="19" height="13" rx="2" />
    <path d="M8 20.5h8M12 16.5v4" />
  </>,
);

export const SearchIcon = icon(
  <>
    <circle cx="11" cy="11" r="7" />
    <path d="m20.5 20.5-4.5-4.5" />
  </>,
);

export const PlusIcon = icon(<path d="M12 5v14M5 12h14" />);
export const MinusIcon = icon(<path d="M5 12h14" />);
export const XIcon = icon(<path d="M18 6 6 18M6 6l12 12" />);
export const CheckIcon = icon(<path d="M20 6 9 17l-5-5" />);
export const ArrowRightIcon = icon(<path d="M5 12h14M13 6l6 6-6 6" />);
export const ChevronRightIcon = icon(<path d="m9 6 6 6-6 6" />);
export const ChevronLeftIcon = icon(<path d="m15 6-6 6 6 6" />);

export const FitIcon = icon(
  <path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3" />,
);

export const RefreshIcon = icon(
  <>
    <path d="M3 12a9 9 0 0 1 15.4-6.4L21 8" />
    <path d="M21 3v5h-5" />
    <path d="M21 12a9 9 0 0 1-15.4 6.4L3 16" />
    <path d="M3 21v-5h5" />
  </>,
);

export const AlertIcon = icon(
  <>
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
    <path d="M12 9v4M12 17h.01" />
  </>,
);

export const BlockedIcon = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="m5.7 5.7 12.6 12.6" />
  </>,
);

export const XCircleIcon = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="m15 9-6 6M9 9l6 6" />
  </>,
);

export const CheckCircleIcon = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="m8.5 12.5 2.5 2.5 5-5.5" />
  </>,
);

export const ClockIcon = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.2 2" />
  </>,
);

export const DriftIcon = icon(
  <>
    <path d="M4 7h11M4 7l3-3M4 7l3 3" />
    <path d="M20 17H9M20 17l-3-3M20 17l-3 3" />
  </>,
);

export const LinkIcon = icon(
  <>
    <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L11.8 5.2" />
    <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" />
  </>,
);

export const DecisionIcon = icon(
  <>
    <path d="M9 18h6M10 21.5h4" />
    <path d="M12 2.5a6.5 6.5 0 0 0-3.8 11.8c.5.4.8 1 .8 1.6V16h6v-.1c0-.6.3-1.2.8-1.6A6.5 6.5 0 0 0 12 2.5z" />
  </>,
);

export const PullRequestIcon = icon(
  <>
    <circle cx="6" cy="5.5" r="2.5" />
    <circle cx="6" cy="18.5" r="2.5" />
    <circle cx="18" cy="18.5" r="2.5" />
    <path d="M6 8v8M18 16V9.5a3 3 0 0 0-3-3h-4" />
    <path d="m13 4-2.5 2.5L13 9" />
  </>,
);

export const MergeIcon = icon(
  <>
    <circle cx="6" cy="5.5" r="2.5" />
    <circle cx="6" cy="18.5" r="2.5" />
    <circle cx="18" cy="12" r="2.5" />
    <path d="M6 8v8M7.5 7.5c1.5 2.8 4.2 4.5 8 4.5" />
  </>,
);

export const DraftIcon = icon(
  <>
    <circle cx="6" cy="5.5" r="2.5" />
    <circle cx="6" cy="18.5" r="2.5" />
    <circle cx="18" cy="18.5" r="2.5" />
    <path d="M6 8v8M18 11v1.5M18 6.5V8" />
  </>,
);

export const MessageIcon = icon(<path d="M21 14.5a2 2 0 0 1-2 2H8l-4.5 4V5a2 2 0 0 1 2-2H19a2 2 0 0 1 2 2z" />);

export const ThreadIcon = icon(
  <>
    <path d="M14.5 9.5a2 2 0 0 1-2 2H7l-3.5 3V4.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2z" />
    <path d="M18 8.5h1a2 2 0 0 1 2 2V21l-3.5-3H12a2 2 0 0 1-2-2v-1" />
  </>,
);

export const TicketIcon = icon(
  <>
    <rect x="3" y="3" width="18" height="18" rx="3.5" />
    <path d="m8 12.2 2.8 2.8L16 9.5" />
  </>,
);

export const UserIcon = icon(
  <>
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  </>,
);

export const UsersIcon = icon(
  <>
    <circle cx="9" cy="8" r="3.5" />
    <path d="M2.5 20.5a6.5 6.5 0 0 1 13 0" />
    <path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M21.5 20.5a6.5 6.5 0 0 0-4-6" />
  </>,
);

export const IdentityIcon = icon(
  <>
    <rect x="3" y="5" width="18" height="14" rx="2.5" />
    <circle cx="9" cy="11" r="2.2" />
    <path d="M5.8 16a3.4 3.4 0 0 1 6.4 0M14.5 10h4M14.5 13.5h3" />
  </>,
);

export const ContainerIcon = icon(<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />);

export const ModuleIcon = icon(
  <>
    <path d="M21 8 12 3 3 8v8l9 5 9-5z" />
    <path d="m3 8 9 5 9-5M12 13v8" />
  </>,
);

export const ActivityIcon = icon(<path d="M22 12h-4l-3 8L9 4l-3 8H2" />);

export const LayersIcon = icon(
  <>
    <path d="m12 3 9.5 4.8L12 12.5 2.5 7.8z" />
    <path d="m2.5 12.2 9.5 4.8 9.5-4.8M2.5 16.4l9.5 4.8 9.5-4.8" />
  </>,
);

export const SparkIcon = icon(
  <>
    <path d="M12 3.5 13.9 9l5.6 2-5.6 2L12 18.5 10.1 13l-5.6-2 5.6-2z" />
    <path d="M19 3v3M17.5 4.5h3" />
  </>,
);

export const InfoIcon = icon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 16v-4.5M12 8h.01" />
  </>,
);

export const EyeIcon = icon(
  <>
    <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
    <circle cx="12" cy="12" r="3" />
  </>,
);

export const EyeOffIcon = icon(
  <>
    <path d="M10 5.2A9.6 9.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-2.2 3.1M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.4 9.4 0 0 0 5.4-1.6" />
    <path d="M14.1 14.1a3 3 0 1 1-4.2-4.2M3 3l18 18" />
  </>,
);

export const LockIcon = icon(
  <>
    <rect x="4" y="10.5" width="16" height="10.5" rx="2.5" />
    <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
  </>,
);

export const FilterIcon = icon(<path d="M3.5 5h17l-6.5 7.5V19l-4 2v-8.5z" />);

export const FlagIcon = icon(<path d="M5 21V4M5 4h12.5l-2.5 4.5 2.5 4.5H5" />);

export const ArrowLeftIcon = icon(<path d="M19 12H5M11 6l-6 6 6 6" />);

export const MenuIcon = icon(<path d="M4 7h16M4 12h16M4 17h16" />);

export const ExternalIcon = icon(
  <>
    <path d="M14 4h6v6M20 4l-9 9" />
    <path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
  </>,
);
