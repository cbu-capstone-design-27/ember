import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getCurrentUser, safeNext, withNext } from "../../../lib/session.ts";
import { SignupForm } from "./signup-form.tsx";

export const metadata: Metadata = { title: "Create your account" };

export default async function SignupPage({ searchParams }: { searchParams: Promise<{ next?: string | string[] }> }) {
  // Where the user was going, e.g. the CLI's /device page, or "" for none (or
  // not a same-site path). Without one, a new account picks its sources next;
  // with one, the dashboard asks for sources later.
  const requested = safeNext((await searchParams).next, "");
  const user = await getCurrentUser();
  if (user) redirect(requested || (user.sources.length ? "/dashboard" : "/onboarding"));
  return <SignupForm next={requested || "/onboarding"} loginHref={withNext("/login", requested)} />;
}
