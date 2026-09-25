"""Local pull emits {type, body} envelopes. No live GitHub calls."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pull_test  # noqa: E402
from validate import assert_valid  # noqa: E402

SOURCE = Path(__file__).resolve().parent / "pull_test.py"


def fake_sign(_pem_path: str, _data: bytes) -> bytes:
    return b"signature-bytes"


class JwtTest(unittest.TestCase):
    def test_claims_and_three_segments(self):
        token = pull_test.build_app_jwt("5075660", "unused.pem", now=1_700_000_000, sign=fake_sign)
        header_b64, payload_b64, signature_b64 = token.split(".")
        self.assertEqual(signature_b64, pull_test.b64url(b"signature-bytes"))

        def decode(segment: str):
            padded = segment + "=" * (-len(segment) % 4)
            return json.loads(pull_test.base64.urlsafe_b64decode(padded))

        header = decode(header_b64)
        payload = decode(payload_b64)
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(payload["iss"], "5075660")
        self.assertEqual(payload["iat"], 1_700_000_000 - 60)
        self.assertEqual(payload["exp"], 1_700_000_000 + 9 * 60)

    def test_script_does_not_hard_code_a_repository(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("ryan-stoffel/photon", text)
        self.assertNotIn("GITHUB_REPO", text)


class LinkTest(unittest.TestCase):
    def test_next_rel(self):
        header = (
            '<https://api.github.com/repos/acme/widget/commits?page=2>; rel="next", '
            '<https://api.github.com/repos/acme/widget/commits?page=5>; rel="last"'
        )
        self.assertEqual(
            pull_test.parse_next_link(header),
            "https://api.github.com/repos/acme/widget/commits?page=2",
        )

    def test_missing_next(self):
        self.assertIsNone(pull_test.parse_next_link(None))
        self.assertIsNone(pull_test.parse_next_link('<https://example.test>; rel="last"'))


class PullFlowTest(unittest.TestCase):
    def exchange(self, method, url, headers, body):
        self.calls.append((method, url, headers.get("Authorization")))
        if url.endswith("/installation"):
            self.assertTrue(headers["Authorization"].startswith("Bearer ey"))
            return pull_test.Response(200, {}, json.dumps({"id": 9}).encode())
        if url.endswith("/access_tokens"):
            self.assertEqual(body, b"{}")
            return pull_test.Response(200, {}, json.dumps({"token": "ghs_test"}).encode())
        if "/issues?" in url:
            self.assertEqual(headers["Authorization"], "Bearer ghs_test")
            return pull_test.Response(200, {}, json.dumps([{"number": 1, "title": "bug"}]).encode())
        if "/pulls?" in url:
            return pull_test.Response(200, {}, json.dumps([{"number": 2, "title": "pr"}]).encode())
        if "/commits?" in url and "page=2" not in url:
            link = '<https://api.github.com/repos/acme/widget/commits?per_page=100&page=2>; rel="next"'
            return pull_test.Response(200, {"Link": link}, json.dumps([{"sha": "aaa"}]).encode())
        if "page=2" in url:
            return pull_test.Response(200, {}, json.dumps([{"sha": "bbb"}]).encode())
        raise AssertionError(url)

    def test_issues_pulls_and_commits_become_envelopes(self):
        self.calls = []
        envelopes = list(
            pull_test.pull(
                "acme/widget",
                "5075660",
                "unused.pem",
                self.exchange,
                now=1_700_000_000,
                sign=fake_sign,
            )
        )
        self.assertEqual(len(envelopes), 4)
        bodies = [item["body"] for item in envelopes]
        self.assertEqual(
            bodies,
            [
                {"number": 1, "title": "bug"},
                {"number": 2, "title": "pr"},
                {"sha": "aaa"},
                {"sha": "bbb"},
            ],
        )
        for envelope in envelopes:
            self.assertEqual(set(envelope), {"type", "body"})
            self.assertEqual(envelope["type"], "github")
            assert_valid(envelope)
        self.assertTrue(any("/repos/acme/widget/installation" in url for _, url, _ in self.calls))

    def test_rejects_repo_without_owner(self):
        with self.assertRaises(pull_test.PullError):
            list(
                pull_test.pull(
                    "photon",
                    "1",
                    "unused.pem",
                    self.exchange,
                    now=1,
                    sign=fake_sign,
                )
            )


if __name__ == "__main__":
    unittest.main()
