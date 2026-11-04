// What the Connect GitHub routes share (EMBER-130), on top of every source's
// (../server.ts): GitHub's callback, notices and cookie path. Server only.

import type { NextRequest, NextResponse } from "next/server";
import { CALLBACK_PATH, COOKIE_PATH, type Candidate } from "../../../lib/github-connect.ts";
import * as connect from "../server.ts";

export { appOrigin, connector, cookieSecret, ingestionPool, onMainAddress, type Connector } from "../server.ts";

export function callbackUrl(request: NextRequest): string {
  return connect.absoluteUrl(request, CALLBACK_PATH);
}

/** Settings, scrolled to Connections, with a notice (settings/page.tsx GITHUB_NOTICES). */
export function settingsUrl(request: NextRequest, notice: string, account?: string): URL {
  return connect.settingsUrl(request, account ? { github: notice, account } : { github: notice });
}

export function cookieOptions(request: NextRequest, maxAge: number) {
  return connect.cookieOptions(request, maxAge, COOKIE_PATH);
}

export function clearCookie(response: NextResponse, name: string) {
  connect.clearCookie(response, name, COOKIE_PATH);
}

/** The existing-installation picker's cookie (PICK_COOKIE): what the callback found, checked against GitHub. */
export interface PickState {
  organizationId: string;
  userId: string;
  candidates: Candidate[];
}
