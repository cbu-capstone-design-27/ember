"use client";

// Browser-side auth calls. Same origin as the page, so the web app and a
// future Electron shell that loads it both work without extra config.
import { deviceAuthorizationClient, inferAdditionalFields } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";
import type { userFields } from "./auth.ts";

export const authClient = createAuthClient({
  plugins: [
    // Types only: the client learns about user.sources without bundling server code.
    inferAdditionalFields<{ user: { additionalFields: typeof userFields } }>(),
    // authClient.device.approve / deny, for the CLI sign-in page (EMBER-81).
    deviceAuthorizationClient(),
  ],
});
