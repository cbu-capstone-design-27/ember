// Seal and open rows of `source_credentials` (ADR 0010, "Credentials";
// EMBER-147). The Python twin, packages/source-credentials/source_credentials.py,
// documents the format; both pass packages/source-credentials/fixtures/vectors.json.
// The web app seals what an owner connects; enrich and intake open it.
//
// Server only: it reads the key rings from the environment.

import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import type pg from "pg";

export type CredentialKind = "api" | "webhook";

export const KEY_RING_ENV: Record<CredentialKind, string> = {
  api: "SOURCE_CREDENTIALS_API_KEYS",
  webhook: "SOURCE_CREDENTIALS_WEBHOOK_KEYS",
};

const KEY_ID = /^[A-Za-z0-9_-]{1,32}$/;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;
const KEY_BYTES = 32;
const NONCE_BYTES = 12;
const TAG_BYTES = 16;
const AAD_PREFIX = "ember:source_credentials:v1";

/** A key ring that can't be read, or a row this ring can't open. Never carries key material. */
export class CredentialError extends Error {}

export interface Sealed {
  keyId: string;
  ciphertext: Buffer;
}

/** The row a credential belongs to, besides its kind: the same pair as `installations`. */
export interface CredentialRow {
  source: string;
  accountId: string;
}

export function additionalData(source: string, accountId: string, kind: CredentialKind): Buffer {
  for (const [name, value] of [["source", source], ["accountId", accountId], ["kind", kind]] as const) {
    if (!value || value.includes("\0")) throw new CredentialError(`${name} must be non-empty and contain no NUL`);
  }
  return Buffer.from([AAD_PREFIX, source, accountId, kind].join("\0"), "utf8");
}

/** The keys for one kind of row. The first seals; any of them opens. */
export class KeyRing {
  readonly kind: CredentialKind;
  readonly current: string;
  readonly #keys: Map<string, Buffer>;

  constructor(kind: CredentialKind, keys: [string, Buffer][]) {
    if (kind !== "api" && kind !== "webhook") throw new CredentialError("kind must be api or webhook");
    if (keys.length === 0) throw new CredentialError(`the ${kind} key ring is empty`);
    this.#keys = new Map();
    for (const [id, key] of keys) {
      if (!KEY_ID.test(id)) throw new CredentialError(`key id ${JSON.stringify(id)} must match ${KEY_ID.source}`);
      if (key.length !== KEY_BYTES) throw new CredentialError(`key ${JSON.stringify(id)} is ${key.length} bytes, not ${KEY_BYTES}`);
      if (this.#keys.has(id)) throw new CredentialError(`the ${kind} key ring repeats a key id`);
      this.#keys.set(id, key);
    }
    this.kind = kind;
    this.current = keys[0][0];
  }

  /** `<key id>:<base64 of 32 bytes>`, comma-separated, the sealing key first. */
  static parse(kind: CredentialKind, value: string): KeyRing {
    const keys: [string, Buffer][] = [];
    for (const raw of value.split(",")) {
      const entry = raw.trim();
      if (!entry) continue;
      const colon = entry.indexOf(":");
      if (colon < 0) throw new CredentialError(`each ${kind} key is <key id>:<base64>`);
      const id = entry.slice(0, colon).trim();
      const encoded = entry.slice(colon + 1).trim();
      // Buffer.from skips what isn't base64, so check first: a typo must not become a different key
      if (!BASE64.test(encoded) || encoded.length % 4 !== 0) throw new CredentialError(`key ${JSON.stringify(id)} is not base64`);
      keys.push([id, Buffer.from(encoded, "base64")]);
    }
    return new KeyRing(kind, keys);
  }

  /** The ring from its environment variable, or null when it is unset. */
  static fromEnv(kind: CredentialKind, env: Record<string, string | undefined> = process.env): KeyRing | null {
    const value = env[KEY_RING_ENV[kind]]?.trim();
    return value ? KeyRing.parse(kind, value) : null;
  }

  get keyIds(): string[] {
    return [...this.#keys.keys()];
  }

  /** Encrypt `secret` for (source, accountId, this ring's kind). `nonce` is for test vectors only. */
  seal({ source, accountId }: CredentialRow, secret: Record<string, unknown>, nonce: Buffer = randomBytes(NONCE_BYTES)): Sealed {
    if (nonce.length !== NONCE_BYTES) throw new CredentialError(`a nonce is ${NONCE_BYTES} bytes`);
    const cipher = createCipheriv("aes-256-gcm", this.#keys.get(this.current)!, nonce, { authTagLength: TAG_BYTES });
    cipher.setAAD(additionalData(source, accountId, this.kind));
    const body = Buffer.concat([cipher.update(JSON.stringify(secret), "utf8"), cipher.final()]);
    return { keyId: this.current, ciphertext: Buffer.concat([nonce, body, cipher.getAuthTag()]) };
  }

  /** Decrypt a row. Fails on an unknown key id, a changed byte, or a row moved to another account. */
  open({ source, accountId }: CredentialRow, keyId: string, ciphertext: Buffer): Record<string, unknown> {
    const key = this.#keys.get(keyId);
    if (!key) throw new CredentialError(`no ${this.kind} key ${JSON.stringify(keyId)} in the ring; was it rotated out too early?`);
    if (ciphertext.length < NONCE_BYTES + TAG_BYTES) throw new CredentialError("ciphertext is too short");
    const decipher = createDecipheriv("aes-256-gcm", key, ciphertext.subarray(0, NONCE_BYTES), { authTagLength: TAG_BYTES });
    decipher.setAAD(additionalData(source, accountId, this.kind));
    decipher.setAuthTag(ciphertext.subarray(ciphertext.length - TAG_BYTES));
    let plaintext: string;
    try {
      plaintext = Buffer.concat([decipher.update(ciphertext.subarray(NONCE_BYTES, ciphertext.length - TAG_BYTES)), decipher.final()]).toString("utf8");
    } catch {
      throw new CredentialError(`the ${source} ${this.kind} credential for ${accountId} doesn't open with key ${JSON.stringify(keyId)}`);
    }
    const value: unknown = JSON.parse(plaintext);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new CredentialError("a credential is a JSON object");
    return value as Record<string, unknown>;
  }
}

/**
 * Seal and store the account's credential of the ring's kind, replacing any
 * it had. The account's `installations` row must exist (foreign key): write
 * the connection first, in the same transaction or before.
 */
export async function storeCredential(db: pg.Pool | pg.PoolClient, ring: KeyRing, row: CredentialRow, secret: Record<string, unknown>) {
  const { keyId, ciphertext } = ring.seal(row, secret);
  await db.query(
    `insert into public.source_credentials (source, account_id, kind, key_id, ciphertext)
     values ($1, $2, $3, $4, $5)
     on conflict (source, account_id, kind) do update
       set key_id = excluded.key_id, ciphertext = excluded.ciphertext, updated_at = now()`,
    [row.source, row.accountId, ring.kind, keyId, ciphertext],
  );
}

/** The account's credential of the ring's kind, opened, or null when none is stored. */
export async function loadCredential(db: pg.Pool | pg.PoolClient, ring: KeyRing, row: CredentialRow): Promise<Record<string, unknown> | null> {
  const { rows } = await db.query<{ key_id: string; ciphertext: Buffer }>(
    "select key_id, ciphertext from public.source_credentials where source = $1 and account_id = $2 and kind = $3",
    [row.source, row.accountId, ring.kind],
  );
  return rows[0] ? ring.open(row, rows[0].key_id, rows[0].ciphertext) : null;
}
