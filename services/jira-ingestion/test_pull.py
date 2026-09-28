"""Local pull emits {type, body} envelopes. No live Jira calls."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pull_test  # noqa: E402
from validate import assert_valid  # noqa: E402

SOURCE = Path(__file__).resolve().parent / "pull_test.py"
ORIGIN = "https://example.atlassian.net"
EMAIL = "dev@example.com"
TOKEN = "api-token-placeholder"

ADF = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Repro: sign in with ?next=/settings"}],
        }
    ],
}

ISSUE_1 = {
    "id": "10001",
    "key": "EMBER-1",
    "fields": {
        "summary": "Login redirect",
        "description": ADF,
        "status": {"name": "In Progress"},
        "project": {"key": "EMBER"},
    },
}

ISSUE_2 = {
    "id": "10002",
    "key": "EMBER-2",
    "fields": {
        "summary": "Empty notes",
        "description": None,
        "status": {"name": "To Do"},
        "project": {"key": "EMBER"},
    },
}


class ProjectTargetTest(unittest.TestCase):
    def test_script_does_not_hard_code_a_site(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("ember-capstone", text)
        self.assertNotIn("atlassian.net", text)
        self.assertNotIn("JIRA_WEBHOOK_SECRET", text)

    def test_default_project_is_ember(self):
        prior = os.environ.pop("JIRA_TEST_PROJECT", None)
        try:
            self.assertEqual(pull_test.DEFAULT_PROJECT, "EMBER")
            self.assertEqual(pull_test.resolve_project(None), "EMBER")
            self.assertEqual(pull_test.resolve_project("WIDGET"), "WIDGET")
        finally:
            if prior is not None:
                os.environ["JIRA_TEST_PROJECT"] = prior

    def test_env_project_overrides_the_default(self):
        prior = os.environ.get("JIRA_TEST_PROJECT")
        os.environ["JIRA_TEST_PROJECT"] = "OTHER"
        try:
            self.assertEqual(pull_test.resolve_project(None), "OTHER")
            self.assertEqual(pull_test.resolve_project("WIDGET"), "WIDGET")
        finally:
            if prior is None:
                os.environ.pop("JIRA_TEST_PROJECT", None)
            else:
                os.environ["JIRA_TEST_PROJECT"] = prior

    def test_rejects_project_key_that_could_change_jql(self):
        with self.assertRaises(pull_test.PullError):
            pull_test.resolve_project("EMBER OR project = OTHER")
        with self.assertRaises(pull_test.PullError):
            pull_test.site_origin("https://example.atlassian.net/rest/api/3")
        with self.assertRaises(pull_test.PullError):
            pull_test.site_origin("http://example.atlassian.net")


class ReadableOutputTest(unittest.TestCase):
    def test_jsonl_stays_one_object_per_line(self):
        import io

        envelopes = [
            {"type": "jira", "body": {"key": "EMBER-1", "fields": {"summary": "Login redirect"}}},
        ]
        buffer = io.StringIO()
        self.assertEqual(pull_test.emit_jsonl(envelopes, buffer), 1)
        line = buffer.getvalue()
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line), envelopes[0])
        assert_valid(envelopes[0])

    def test_summary_uses_adf_description_and_skips_null(self):
        envelopes = [
            {"type": "jira", "body": ISSUE_1},
            {"type": "jira", "body": ISSUE_2},
            {
                "type": "jira",
                "body": {
                    "key": "EMBER-3",
                    "fields": {
                        "summary": "Notes",
                        "description": "First line.\n\nSecond line stays out of the excerpt when long "
                        "enough to pass the limit and then some more words.",
                        "status": {"name": "Done"},
                    },
                },
            },
        ]
        text = pull_test.render_summary("EMBER", envelopes)
        self.assertIn("# EMBER", text)
        issues = text.split("## Issues", 1)[1]
        self.assertIn(
            "- EMBER-1 In Progress — Login redirect — Repro: sign in with ?next=/settings",
            issues,
        )
        self.assertIn("- EMBER-2 To Do — Empty notes\n", issues)
        self.assertNotIn("None", issues)
        self.assertIn("- EMBER-3 Done — Notes — First line. Second line stays out", issues)
        self.assertNotIn('"type": "doc"', text)

    def test_sidecar_path_sits_beside_jsonl(self):
        self.assertEqual(
            pull_test.readable_sidecar(Path("/tmp/intake.jsonl")),
            Path("/tmp/intake.jsonl.readable.md"),
        )


class PullFlowTest(unittest.TestCase):
    def exchange(self, method, url, headers, body):
        self.calls.append((method, url, headers.get("Authorization"), body))
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertNotIn(TOKEN, headers["Authorization"])
        if url.endswith("/rest/api/3/search/jql"):
            self.assertEqual(method, "POST")
            payload = json.loads(body)
            self.assertEqual(payload["jql"], "project = EMBER ORDER BY key ASC")
            self.assertEqual(payload["fields"], ["key"])
            self.assertEqual(payload["maxResults"], 100)
            if "nextPageToken" not in payload:
                return pull_test.Response(
                    200,
                    {},
                    json.dumps(
                        {
                            "issues": [{"id": "10001", "key": "EMBER-1"}],
                            "nextPageToken": "page-2",
                            "isLast": False,
                        }
                    ).encode(),
                )
            self.assertEqual(payload["nextPageToken"], "page-2")
            return pull_test.Response(
                200,
                {},
                json.dumps({"issues": [{"id": "10002", "key": "EMBER-2"}], "isLast": True}).encode(),
            )
        if url.endswith("/rest/api/3/issue/EMBER-1"):
            self.assertEqual(method, "GET")
            return pull_test.Response(200, {}, json.dumps(ISSUE_1).encode())
        if url.endswith("/rest/api/3/issue/EMBER-2"):
            return pull_test.Response(200, {}, json.dumps(ISSUE_2).encode())
        raise AssertionError(url)

    def test_issues_become_envelopes_with_full_description(self):
        self.calls = []
        envelopes = list(pull_test.pull(ORIGIN, EMAIL, TOKEN, "EMBER", self.exchange))
        self.assertEqual(len(envelopes), 2)
        self.assertEqual(envelopes[0]["body"], ISSUE_1)
        self.assertEqual(envelopes[0]["body"]["fields"]["description"], ADF)
        self.assertIsNone(envelopes[1]["body"]["fields"]["description"])
        detail_urls = [url for _, url, _, _ in self.calls]
        self.assertIn(f"{ORIGIN}/rest/api/3/issue/EMBER-1", detail_urls)
        self.assertIn(f"{ORIGIN}/rest/api/3/issue/EMBER-2", detail_urls)
        self.assertEqual(detail_urls[0], f"{ORIGIN}/rest/api/3/search/jql")
        for envelope in envelopes:
            self.assertEqual(set(envelope), {"type", "body"})
            self.assertEqual(envelope["type"], "jira")
            assert_valid(envelope)

    def test_rejects_bad_project_before_http(self):
        self.calls = []
        with self.assertRaises(pull_test.PullError):
            list(pull_test.pull(ORIGIN, EMAIL, TOKEN, "ember", self.exchange))
        self.assertEqual(self.calls, [])


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_results_stay_in_list_order(self):
        import time

        def work(number: int) -> int:
            time.sleep(0.02 if number == 1 else 0)
            return number

        self.assertEqual(pull_test.map_ordered(work, [1, 2, 3, 4], 4), [1, 2, 3, 4])

    def test_retry_after_is_waited_then_the_body_is_kept(self):
        calls = {"n": 0}

        def exchange(method, url, headers, body):
            calls["n"] += 1
            if calls["n"] == 1:
                return pull_test.Response(429, {"Retry-After": "2"}, b"slow down")
            return pull_test.Response(
                200,
                {},
                json.dumps({"key": "EMBER-1", "fields": {"description": "kept"}}).encode(),
            )

        waits: list[float] = []
        response = pull_test.exchange_with_retry(
            exchange,
            "GET",
            f"{ORIGIN}/rest/api/3/issue/EMBER-1",
            {},
            None,
            sleep=waits.append,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(waits, [2.0])
        self.assertEqual(response.json()["fields"]["description"], "kept")

    def test_default_concurrency_is_twelve(self):
        prior = os.environ.pop("JIRA_PULL_CONCURRENCY", None)
        try:
            self.assertEqual(pull_test.DEFAULT_CONCURRENCY, 12)
            self.assertEqual(pull_test.resolve_concurrency(None), 12)
            self.assertEqual(pull_test.resolve_concurrency(8), 8)
        finally:
            if prior is not None:
                os.environ["JIRA_PULL_CONCURRENCY"] = prior

    def test_env_concurrency_is_used_when_cli_is_absent(self):
        prior = os.environ.get("JIRA_PULL_CONCURRENCY")
        os.environ["JIRA_PULL_CONCURRENCY"] = "4"
        try:
            self.assertEqual(pull_test.resolve_concurrency(None), 4)
        finally:
            if prior is None:
                os.environ.pop("JIRA_PULL_CONCURRENCY", None)
            else:
                os.environ["JIRA_PULL_CONCURRENCY"] = prior


if __name__ == "__main__":
    unittest.main()
