"""Seal and open rows of `source_credentials` (ADR 0010, "Credentials"; EMBER-147).

Every row is AES-256-GCM ciphertext made by the application, so Postgres, its backups and anyone who
reads the table see no token. The web app's TypeScript twin is apps/web/src/lib/source-credentials.ts.
Both must read and write fixtures/vectors.json the same way.

Format:

- **Key ring.** One per kind, from an environment variable: `SOURCE_CREDENTIALS_API_KEYS` for `api`
  rows (web app and enrich), `SOURCE_CREDENTIALS_WEBHOOK_KEYS` for `webhook` rows (web app and intake).
  The value is `<key id>:<base64 of 32 bytes>`, comma-separated. The first key seals; every key opens.
  A rotation puts the new key first, re-seals the rows whose `key_id` is an old one, then drops it.
- **`ciphertext`** is the 12-byte nonce, the ciphertext, then the 16-byte tag.
- **Additional data** is `ember:source_credentials:v1`, the source, the account id and the kind,
  NUL-separated. A row copied to another account, or to the other kind, fails to open.
- **Plaintext** is a JSON object, e.g. `{"access_token": ..., "refresh_token": ...}`.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KINDS = ("api", "webhook")
ENV = {"api": "SOURCE_CREDENTIALS_API_KEYS", "webhook": "SOURCE_CREDENTIALS_WEBHOOK_KEYS"}
KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
AAD_PREFIX = b"ember:source_credentials:v1"


class CredentialError(Exception):
    """A key ring that can't be read, or a row this ring can't open. Never carries key material."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    ciphertext: bytes


def additional_data(source: str, account_id: str, kind: str) -> bytes:
    for name, value in (("source", source), ("account_id", account_id), ("kind", kind)):
        if not value or "\0" in value:
            raise CredentialError(f"{name} must be non-empty and contain no NUL")
    return b"\0".join([AAD_PREFIX, source.encode("utf-8"), account_id.encode("utf-8"), kind.encode("utf-8")])


class KeyRing:
    """The keys for one kind of row. The first seals; any of them opens."""

    def __init__(self, kind: str, keys: list[tuple[str, bytes]]) -> None:
        if kind not in KINDS:
            raise CredentialError(f"kind must be one of {KINDS}")
        if not keys:
            raise CredentialError(f"the {kind} key ring is empty")
        ids = [key_id for key_id, _ in keys]
        if len(set(ids)) != len(ids):
            raise CredentialError(f"the {kind} key ring repeats a key id")
        for key_id, key in keys:
            if not KEY_ID.match(key_id):
                raise CredentialError(f"key id {key_id!r} must match {KEY_ID.pattern}")
            if len(key) != KEY_BYTES:
                raise CredentialError(f"key {key_id!r} is {len(key)} bytes, not {KEY_BYTES}")
        self.kind = kind
        self._keys = dict(keys)
        self.current = ids[0]

    @classmethod
    def parse(cls, kind: str, value: str) -> "KeyRing":
        keys: list[tuple[str, bytes]] = []
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            key_id, sep, encoded = entry.partition(":")
            if not sep:
                raise CredentialError(f"each {kind} key is <key id>:<base64>")
            try:
                key = base64.b64decode(encoded.strip(), validate=True)
            except (binascii.Error, ValueError):
                raise CredentialError(f"key {key_id.strip()!r} is not base64") from None
            keys.append((key_id.strip(), key))
        return cls(kind, keys)

    @classmethod
    def from_env(cls, kind: str, environ: Mapping[str, str] = os.environ) -> "KeyRing | None":
        """The ring from its environment variable, or None when it is unset."""
        value = environ.get(ENV[kind], "").strip()
        return cls.parse(kind, value) if value else None

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def seal(self, source: str, account_id: str, secret: Mapping[str, object], *, nonce: bytes | None = None) -> Sealed:
        """Encrypt `secret` for the row (source, account_id, this ring's kind). `nonce` is for test vectors only."""
        nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
        if len(nonce) != NONCE_BYTES:
            raise CredentialError(f"a nonce is {NONCE_BYTES} bytes")
        plaintext = json.dumps(dict(secret), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        sealed = AESGCM(self._keys[self.current]).encrypt(nonce, plaintext, additional_data(source, account_id, self.kind))
        return Sealed(self.current, nonce + sealed)

    def open(self, source: str, account_id: str, key_id: str, ciphertext: bytes) -> dict:
        """Decrypt a row. Fails on an unknown key id, a changed byte, or a row moved to another account."""
        key = self._keys.get(key_id)
        if key is None:
            raise CredentialError(f"no {self.kind} key {key_id!r} in the ring; was it rotated out too early?")
        ciphertext = bytes(ciphertext)
        if len(ciphertext) < NONCE_BYTES + TAG_BYTES:
            raise CredentialError("ciphertext is too short")
        try:
            plaintext = AESGCM(key).decrypt(
                ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:], additional_data(source, account_id, self.kind))
        except InvalidTag:
            raise CredentialError(f"the {source} {self.kind} credential for {account_id} doesn't open with key {key_id!r}") from None
        value = json.loads(plaintext)
        if not isinstance(value, dict):
            raise CredentialError("a credential is a JSON object")
        return value
