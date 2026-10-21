import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getCurrentUser, safeNext } from "../../../lib/session.ts";
import { SignupForm } from "./signup-form.tsx";

export const metadata: Metadata = { title: "Create your account" };

export default async function SignupPage({ searchParams }: { searchParams: Promise<{ next?: string | string[] }> }) {
  const requested = (await searchParams).next;
  // A new account picks its sources next, unless it came from somewhere, e.g.
  // the CLI's /device page. The dashboard asks for sources later in that case.
  const next = safeNext(requested, "/onboarding");
  const user = await getCurrentUser();
  if (user) redirect(requested ? next : user.sources.length ? "/dashboard" : "/onboarding");
  return <SignupForm next={next} loginHref={requested ? `/login?next=${encodeURIComponent(next)}` : "/login"} />;
}
