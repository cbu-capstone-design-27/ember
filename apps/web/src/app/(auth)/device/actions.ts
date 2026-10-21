"use server";

// Approve and Deny on /device (EMBER-81), run on the server with the visitor's session.
import { headers } from "next/headers";
import { getAuth } from "../../../lib/auth.ts";
import { decideDeviceRequest, type DeviceDecision } from "../../../lib/device.ts";

export async function decide(userCode: string, action: "approve" | "deny"): Promise<DeviceDecision> {
  // A server action is a public endpoint: check what the browser sent.
  if (typeof userCode !== "string" || !userCode || (action !== "approve" && action !== "deny")) {
    throw new Error("decide needs a user code and approve or deny");
  }
  return decideDeviceRequest(getAuth(), userCode, action, await headers());
}
