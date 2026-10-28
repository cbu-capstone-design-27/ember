// The /device page for codes from `ember auth login` (EMBER-81). Showing a
// code binds it to no one: the first account to approve or deny it claims it.
// A user who opened the link while signed into the wrong account can still
// switch accounts before deciding.

import { isAPIError } from "better-auth/api";
import type { Auth } from "./auth.ts";

export type DeviceRequest =
  | { state: "pending"; userCode: string }
  | { state: "approved" | "denied" | "expired" | "invalid" };

/** What /device shows for a code. Looked up without the visitor's session, so nothing is claimed. */
export async function findDeviceRequest(auth: Auth, userCode: string): Promise<DeviceRequest> {
  try {
    const request = await auth.api.deviceVerify({ query: { user_code: userCode }, headers: new Headers() });
    if (request.status === "pending") return { state: "pending", userCode: request.user_code };
    return { state: request.status === "approved" ? "approved" : "denied" };
  } catch (error) {
    if (!isAPIError(error)) throw error;
    return { state: errorCode(error) === "expired_token" ? "expired" : "invalid" };
  }
}

export type DeviceDecision =
  | { state: "approved" | "denied" | "expired" }
  /** Another account claimed the code first. */
  | { state: "taken" }
  /** The session ended after the page loaded. */
  | { state: "signed-out" }
  /** Decided elsewhere, used or gone: show the page again to see which. */
  | { state: "changed" };

/**
 * Approve or deny a code as the user whose session `headers` carry. The code
 * is claimed for them first, unless another account already has it.
 */
export async function decideDeviceRequest(
  auth: Auth,
  userCode: string,
  action: "approve" | "deny",
  headers: Headers,
): Promise<DeviceDecision> {
  try {
    await auth.api.deviceVerify({ query: { user_code: userCode }, headers });
    if (action === "approve") await auth.api.deviceApprove({ body: { userCode }, headers });
    else await auth.api.deviceDeny({ body: { userCode }, headers });
    return { state: action === "approve" ? "approved" : "denied" };
  } catch (error) {
    if (!isAPIError(error)) throw error;
    if (error.statusCode === 401) return { state: "signed-out" };
    const code = errorCode(error);
    if (code === "expired_token") return { state: "expired" };
    if (code === "access_denied") return { state: "taken" };
    return { state: "changed" };
  }
}

function errorCode(error: { body?: unknown }): string | undefined {
  return (error.body as { error?: string } | undefined)?.error;
}

/** ABCDEFGH -> ABCD-EFGH, the way the CLI prints it, so the two are easy to compare. */
export function formatUserCode(userCode: string): string {
  const code = userCode.replace(/[^a-zA-Z0-9]/g, "").toUpperCase();
  return code.match(/.{1,4}/g)?.join("-") ?? code;
}
