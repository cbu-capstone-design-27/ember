// Geist for all text, Geist Mono for technical values (issue keys, SHAs,
// timestamps, commands, source ids). Self-hosted through next/font from
// ./fonts (SIL OFL), so nothing loads from a CDN.

import localFont from "next/font/local";

export const sans = localFont({
  src: "./fonts/Geist-Variable.woff2",
  weight: "100 900",
  variable: "--font-geist",
  display: "swap",
});

export const mono = localFont({
  src: "./fonts/GeistMono-Variable.woff2",
  weight: "100 900",
  variable: "--font-geist-mono",
  display: "swap",
});
