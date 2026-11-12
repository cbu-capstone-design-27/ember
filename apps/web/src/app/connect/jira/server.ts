// What the Connect Jira routes and picker share (EMBER-131), on top of every
// source's (../server.ts): the callback, notices, cookie path, and the stores
// a consent needs. Server only.

import type { NextRequest, NextResponse } from "next/server";
import { getAuth } from "../../../lib/auth.ts";
import { CALLBACK_PATH, COOKIE_PATH, type VerificationStore } from "../../../lib/jira-connect.ts";
import * as connect from "../server.ts";

export function callbackUrl(request: NextRequest): string {
  return connect.absoluteUrl(request, CALLBACK_PATH);
}

/** Settings, scrolled to Connections, with a notice (settings/page.tsx JIRA_NOTICES). */
export function settingsUrl(request: NextRequest, notice: string, account?: string): URL {
  return connect.settingsUrl(request, account ? { jira: notice, account } : { jira: notice });
}

export function cookieOptions(request: NextRequest, maxAge: number) {
  return connect.cookieOptions(request, maxAge, COOKIE_PATH);
}

export function clearCookie(response: NextResponse, name: string) {
  connect.clearCookie(response, name, COOKIE_PATH);
}

/** The api key ring the token pair is sealed with (shared with Connect Slack). */
export { apiKeyRing } from "../server.ts";

/** Where a consent waits for the picker: Better Auth's verification table. */
export async function verificationStore(): Promise<VerificationStore> {
  return (await getAuth().$context).internalAdapter as unknown as VerificationStore;
}
