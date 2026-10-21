// Where `ember auth login` sends the browser (EMBER-81). The user signs in (or
// creates an account), checks the code matches the one in their terminal, and
// approves or denies. The CLI is polling the whole time.

import type { Metadata } from "next";
import { getAuth } from "../../../lib/auth.ts";
import { findDeviceRequest, formatUserCode } from "../../../lib/device.ts";
import { requireUser } from "../../../lib/session.ts";
import { DeviceApproval, DeviceCodeForm, DeviceMessage } from "./device-form.tsx";

export const metadata: Metadata = { title: "Sign in to the Ember CLI" };

export default async function DevicePage({ searchParams }: { searchParams: Promise<{ user_code?: string | string[] }> }) {
  const value = (await searchParams).user_code;
  const userCode = (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
  const here = userCode ? `/device?user_code=${encodeURIComponent(userCode)}` : "/device";
  const user = await requireUser(here);
  if (!userCode) return <DeviceCodeForm />;

  const request = await findDeviceRequest(getAuth(), userCode);
  if (request.state !== "pending") return <DeviceMessage state={request.state} />;
  return <DeviceApproval userCode={request.userCode} code={formatUserCode(request.userCode)} email={user.email} here={here} />;
}
