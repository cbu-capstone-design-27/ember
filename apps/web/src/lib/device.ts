// What /device shows for a code from `ember auth login` (EMBER-81). Opening the
// page claims a pending code for the signed-in user; after that only they see
// it and can approve or deny it.

import { isAPIError } from "better-auth/api";
import type { Auth } from "./auth.ts";

export type DeviceRequest =
  | { state: "pending"; userCode: string }
  | { state: "approved" | "denied" | "expired" | "invalid" };

export async function openDeviceRequest(auth: Auth, userCode: string, headers: Headers): Promise<DeviceRequest> {
  try {
    const request = await auth.api.deviceVerify({ query: { user_code: userCode }, headers });
    // client_id comes back only to the user who claimed the code. Anyone else
    // gets the same answer as for a code that doesn't exist.
    if (!request.client_id) return { state: "invalid" };
    if (request.status === "pending") return { state: "pending", userCode: request.user_code };
    return { state: request.status === "approved" ? "approved" : "denied" };
  } catch (error) {
    if (!isAPIError(error)) throw error;
    return { state: (error.body as { error?: string } | undefined)?.error === "expired_token" ? "expired" : "invalid" };
  }
}

/** ABCDEFGH -> ABCD-EFGH, the way the CLI prints it, so the two are easy to compare. */
export function formatUserCode(userCode: string): string {
  const code = userCode.replace(/[^a-zA-Z0-9]/g, "").toUpperCase();
  return code.match(/.{1,4}/g)?.join("-") ?? code;
}
