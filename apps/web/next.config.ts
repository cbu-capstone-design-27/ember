import type { NextConfig } from "next";

const config: NextConfig = {
  // Self-contained server in .next/standalone, for the container image.
  output: "standalone",
  // Keep the Postgres driver out of the bundle; it is loaded at runtime.
  serverExternalPackages: ["pg"],
};

export default config;
