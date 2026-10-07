// Runs once when the server starts: create or update the auth tables in
// whichever database DATABASE_URL points at, before anything serves a request.
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { migrateDatabase } = await import("./lib/auth.ts");
  await migrateDatabase();
}
