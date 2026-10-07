import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { THEME_SCRIPT } from "../components/theme-script.ts";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Ember", template: "%s · Ember" },
  description: "Your team's context, connected: GitHub, Jira and Slack in one knowledge graph.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f7f6f4" },
    { media: "(prefers-color-scheme: dark)", color: "#0e0d0c" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // data-theme is set by THEME_SCRIPT before React hydrates.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
