import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { mono, sans } from "./fonts.ts";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Ember", template: "%s · Ember" },
  description: "GitHub, Jira and Slack in one knowledge graph, and where they disagree.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f7f7f5" },
    { media: "(prefers-color-scheme: dark)", color: "#09090b" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // Light or dark follows the system (prefers-color-scheme in globals.css).
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
