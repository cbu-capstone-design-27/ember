// What the Connect Slack routes share (EMBER-148), on top of every source's
// (../server.ts): Slack's redirect URL, notices and cookie path. Server only.

import type { NextRequest, NextResponse } from "next/server";
import { CALLBACK_PATH, COOKIE_PATH } from "../../../lib/slack-connect.ts";
import * as connect from "../server.ts";

export function callbackUrl(request: NextRequest): string {
  return connect.absoluteUrl(request, CALLBACK_PATH);
}

/**
 * Settings, scrolled to Connections, with a notice (settings/page.tsx
 * SLACK_NOTICES). `account` is a team id: Settings shows the workspace's name
 * from the tenant's own connections, so the URL can't put other text on the page.
 */
export function settingsUrl(request: NextRequest, notice: string, account?: string): URL {
  return connect.settingsUrl(request, account ? { slack: notice, account } : { slack: notice });
}

export function cookieOptions(request: NextRequest, maxAge: number) {
  return connect.cookieOptions(request, maxAge, COOKIE_PATH);
}

export function clearCookie(response: NextResponse, name: string) {
  connect.clearCookie(response, name, COOKIE_PATH);
}
