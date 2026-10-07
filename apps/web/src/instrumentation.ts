// Runs once when the server starts: create or update the auth tables in
// whichever database DATABASE_URL points at, before anything serves a request.
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  // Without a secret, Better Auth would fail on the first request instead;
  // stop here with a message that says what to do.
  if (process.env.NODE_ENV === "production" && !process.env.BETTER_AUTH_SECRET) {
    throw new Error("BETTER_AUTH_SECRET is not set. Generate one with `openssl rand -hex 32` and put it in .env.");
  }
  const { migrateDatabase } = await import("./lib/auth.ts");
  await migrateDatabase();
}
