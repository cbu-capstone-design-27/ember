import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getCurrentUser } from "../../../lib/session.ts";
import { SignupForm } from "./signup-form.tsx";

export const metadata: Metadata = { title: "Create your account" };

export default async function SignupPage() {
  const user = await getCurrentUser();
  if (user) redirect(user.sources.length ? "/dashboard" : "/onboarding");
  return <SignupForm />;
}
