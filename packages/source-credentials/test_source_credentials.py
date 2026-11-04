"""EMBER-147: source_credentials rows open only with the right key, for the account and kind they were sealed for.

fixtures/vectors.json is shared with the web app's test (apps/web/test/source-credentials.test.ts), so
a row the web app seals is one enrich and intake can open. Needs `cryptography`.

    python3 packages/source-credentials/test_source_credentials.py
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from source_credentials import CredentialError, KeyRing  # noqa: E402

VECTORS = json.loads((HERE / "fixtures" / "vectors.json").read_text())
KEY = base64.b64encode(bytes(range(32))).decode()


class VectorsTest(unittest.TestCase):
    def test_every_vector_opens_and_seals_to_the_same_bytes(self):
        for case in VECTORS["cases"]:
            with self.subTest(source=case["source"], kind=case["kind"]):
                ring = KeyRing.parse(case["kind"], VECTORS["rings"][case["kind"]])
                ciphertext = base64.b64decode(case["ciphertext"])
                self.assertEqual(ring.open(case["source"], case["account_id"], case["key_id"], ciphertext), case["secret"])
                only = KeyRing.parse(case["kind"], next(
                    e for e in VECTORS["rings"][case["kind"]].split(",") if e.strip().startswith(case["key_id"] + ":")))
                sealed = only.seal(case["source"], case["account_id"], case["secret"], nonce=base64.b64decode(case["nonce"]))
                self.assertEqual((sealed.key_id, sealed.ciphertext), (case["key_id"], ciphertext))

    def test_a_row_moved_to_another_account_or_kind_does_not_open(self):
        case = VECTORS["cases"][0]
        ring = KeyRing.parse("api", VECTORS["rings"]["api"])
        ciphertext = base64.b64decode(case["ciphertext"])
        with self.assertRaises(CredentialError):
            ring.open(case["source"], "evil.atlassian.net", case["key_id"], ciphertext)
        with self.assertRaises(CredentialError):
            ring.open("slack", case["account_id"], case["key_id"], ciphertext)
        # The webhook ring, given the same key bytes, still refuses an api row: the kind is in the additional data
        same_key = KeyRing.parse("webhook", f"{case['key_id']}:{VECTORS['rings']['api'].split(',')[0].split(':', 1)[1]}")
        with self.assertRaises(CredentialError):
            same_key.open(case["source"], case["account_id"], case["key_id"], ciphertext)

    def test_a_changed_byte_or_unknown_key_does_not_open(self):
        case = VECTORS["cases"][0]
        ring = KeyRing.parse("api", VECTORS["rings"]["api"])
        ciphertext = bytearray(base64.b64decode(case["ciphertext"]))
        ciphertext[-1] ^= 1
        with self.assertRaises(CredentialError):
            ring.open(case["source"], case["account_id"], case["key_id"], bytes(ciphertext))
        with self.assertRaises(CredentialError) as raised:
            ring.open(case["source"], case["account_id"], "retired", base64.b64decode(case["ciphertext"]))
        self.assertIn("rotated out", str(raised.exception))
        with self.assertRaises(CredentialError):
            ring.open(case["source"], case["account_id"], case["key_id"], b"short")


class KeyRingTest(unittest.TestCase):
    def test_the_first_key_seals_and_a_fresh_nonce_is_used_each_time(self):
        ring = KeyRing.parse("api", VECTORS["rings"]["api"])
        self.assertEqual(ring.current, "2026-11")
        self.assertEqual(ring.key_ids, ("2026-11", "2026-10"))
        one = ring.seal("jira", "acme.atlassian.net", {"access_token": "x"})
        two = ring.seal("jira", "acme.atlassian.net", {"access_token": "x"})
        self.assertNotEqual(one.ciphertext, two.ciphertext)
        self.assertEqual(ring.open("jira", "acme.atlassian.net", one.key_id, one.ciphertext), {"access_token": "x"})

    def test_from_env_reads_the_kind_s_own_variable(self):
        self.assertIsNone(KeyRing.from_env("api", {}))
        self.assertIsNone(KeyRing.from_env("api", {"SOURCE_CREDENTIALS_WEBHOOK_KEYS": f"w:{KEY}"}))
        self.assertEqual(KeyRing.from_env("webhook", {"SOURCE_CREDENTIALS_WEBHOOK_KEYS": f" w:{KEY}\n"}).current, "w")

    def test_a_bad_ring_is_refused_without_echoing_the_key(self):
        bad = {
            "not base64": "k:not-base64!!",
            "short key": f"k:{base64.b64encode(bytes(16)).decode()}",
            "no id": KEY,
            "bad id": f"has space:{KEY}",
            "repeated id": f"k:{KEY},k:{KEY}",
            "empty": " , ",
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(CredentialError) as raised:
                KeyRing.parse("api", value)
            self.assertNotIn(KEY, str(raised.exception))
        with self.assertRaises(CredentialError):
            KeyRing("other", [("k", bytes(32))])

    def test_parts_with_nul_are_refused(self):
        ring = KeyRing.parse("api", f"k:{KEY}")
        with self.assertRaises(CredentialError):
            ring.seal("jira", "a\0b", {})
        with self.assertRaises(CredentialError):
            ring.seal("", "acme", {})


if __name__ == "__main__":
    unittest.main()
