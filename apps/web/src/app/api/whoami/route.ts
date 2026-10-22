// Who a CLI token belongs to (EMBER-81). `ember auth login` and `ember auth status`
// send the token from /api/auth/device/token as `Authorization: Bearer <token>`.
import { getAuth, whoami } from "../../../lib/auth.ts";

export async function GET(request: Request) {
  const user = await whoami(getAuth(), request.headers);
  const headers = { "cache-control": "no-store" };
  if (!user) {
    return Response.json(
      { error: "unauthorized", error_description: "Missing, invalid or expired token. Run `ember auth login`." },
      { status: 401, headers },
    );
  }
  return Response.json(user, { headers });
}
