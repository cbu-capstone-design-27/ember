// Every auth endpoint (sign-up, sign-in, sign-out, session) under /api/auth/*.
import { toNextJsHandler } from "better-auth/next-js";
import { getAuth } from "../../../../lib/auth.ts";

export const { GET, POST } = toNextJsHandler((request: Request) => getAuth().handler(request));
