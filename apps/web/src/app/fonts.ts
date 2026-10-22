// The two typefaces, self-hosted through next/font (files in ./fonts, SIL OFL).
// Instrument Serif sets display type: page titles, big numbers, the landing
// headline. Instrument Sans, from the same foundry, sets everything else.
// Code and ticket keys use the system monospace.

import localFont from "next/font/local";

export const sans = localFont({
  src: "./fonts/InstrumentSans-Variable.woff2",
  weight: "400 700",
  variable: "--font-instrument-sans",
  display: "swap",
});

export const serif = localFont({
  src: [
    { path: "./fonts/InstrumentSerif-Regular.woff2", style: "normal", weight: "400" },
    { path: "./fonts/InstrumentSerif-Italic.woff2", style: "italic", weight: "400" },
  ],
  variable: "--font-instrument-serif",
  display: "swap",
});
