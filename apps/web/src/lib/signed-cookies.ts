// Signed cookies for the connect round trips (ADR 0010). The browser holds
// them, so they are signed: otherwise a person could point their own round
// trip at another organization or add an account to a picker. Each source
// signs with its own key, derived from BETTER_AUTH_SECRET and a purpose, so a
// cookie one flow issued means nothing to another.
//
// Server only.

import { createHmac, timingSafeEqual } from "node:crypto";

export interface CookieSealer {
  /** `value` as `<payload>.<signature>`, valid for `ttlSeconds`. */
  seal(value: object, secret: string, ttlSeconds: number, now?: number): string;
  /** What `seal` stored, or null when the value is missing, altered or expired. */
  unseal<T extends object>(sealed: string | undefined, secret: string, now?: number): T | null;
}

export function cookieSealer(purpose: string): CookieSealer {
  const mac = (payload: string, secret: string) => {
    const key = createHmac("sha256", secret).update(purpose).digest();
    return createHmac("sha256", key).update(payload).digest("base64url");
  };
  return {
    seal(value, secret, ttlSeconds, now = Date.now()) {
      const payload = Buffer.from(JSON.stringify({ ...value, exp: Math.floor(now / 1000) + ttlSeconds })).toString("base64url");
      return `${payload}.${mac(payload, secret)}`;
    },
    unseal<T extends object>(sealed: string | undefined, secret: string, now = Date.now()): T | null {
      if (!sealed) return null;
      const [payload, signature, extra] = sealed.split(".");
      if (!payload || !signature || extra !== undefined) return null;
      const expected = Buffer.from(mac(payload, secret));
      const given = Buffer.from(signature);
      if (expected.length !== given.length || !timingSafeEqual(expected, given)) return null;
      let value: (T & { exp?: unknown }) | null;
      try {
        value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
      } catch {
        return null;
      }
      if (!value || typeof value !== "object" || typeof value.exp !== "number" || value.exp * 1000 <= now) return null;
      return value;
    },
  };
}

/** Whether `returned` is the state `expected` holds, in constant time. */
export function sameState(expected: string, returned: string | null): boolean {
  if (!returned) return false;
  const a = Buffer.from(expected);
  const b = Buffer.from(returned);
  return a.length === b.length && timingSafeEqual(a, b);
}
