"use client";

// Browser-side auth calls. Same origin as the page, so the web app and a
// future Electron shell that loads it both work without extra config.
import { createAuthClient } from "better-auth/react";

export const authClient = createAuthClient();
