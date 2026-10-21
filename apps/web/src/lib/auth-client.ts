"use client";

// Browser-side auth calls. Same origin as the page, so the web app and a
// future Electron shell that loads it both work without extra config.
import { inferAdditionalFields } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";
import type { userFields } from "./auth.ts";

export const authClient = createAuthClient({
  // Types only: the client learns about user.sources without bundling server code.
  plugins: [inferAdditionalFields<{ user: { additionalFields: typeof userFields } }>()],
});
