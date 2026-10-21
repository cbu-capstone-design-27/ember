import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getCurrentUser, safeNext } from "../../../lib/session.ts";
import { LoginForm } from "./login-form.tsx";

export const metadata: Metadata = { title: "Log in" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string | string[] }> }) {
  const requested = (await searchParams).next;
  const next = safeNext(requested);
  if (await getCurrentUser()) redirect(next);
  // Creating an account instead keeps where the user was going, e.g. the CLI's /device page.
  return <LoginForm next={next} signupHref={requested ? `/signup?next=${encodeURIComponent(next)}` : "/signup"} />;
}
