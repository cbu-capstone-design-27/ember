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

COMMENT_A = {
    "id": "100",
    "self": f"{ORIGIN}/rest/api/3/issue/10001/comment/100",
    "body": "First note",
    "created": "2026-09-01T00:00:00.000+0000",
    "updateAuthor": {"displayName": "Ada"},
}
COMMENT_B = {
    "id": "101",
    "self": f"{ORIGIN}/rest/api/3/issue/10001/comment/101",
    "body": ADF,
    "created": "2026-09-02T00:00:00.000+0000",
    "updateAuthor": {"displayName": "Ada"},
}
WORKLOG = {
    "id": "200",
    "self": f"{ORIGIN}/rest/api/3/issue/10001/worklog/200",
    "timeSpent": "1h",
    "timeSpentSeconds": 3600,
    "started": "2026-09-01T00:00:00.000+0000",
    "comment": "Did the work",
}
CHANGELOG = {
    "startAt": 0,
    "maxResults": 100,
    "total": 1,
    "isLast": True,
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-1/changelog?startAt=0",
    "histories": [{"id": "1", "items": [{"field": "summary", "toString": "Login redirect"}]}],
}
CHANGELOG_2 = {
    "startAt": 0,
    "maxResults": 100,
    "total": 0,
    "isLast": True,
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-2/changelog?startAt=0",
    "histories": [],
}
ATTACHMENT = {
    "id": "55",
    "filename": "shot.png",
    "mimeType": "image/png",
    "size": 12,
    "content": f"{ORIGIN}/rest/api/3/attachment/content/55",
    "self": f"{ORIGIN}/rest/api/3/attachment/55",
}
LINK = {
    "id": "77",
    "self": f"{ORIGIN}/rest/api/3/issueLink/77",
    "type": {"name": "Relates"},
    "outwardIssue": {"key": "EMBER-2"},
}
REMOTE = {
    "id": 9,
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-1/remotelink/9",
    "relationship": "causes",
    "object": {"url": "https://example.test/doc", "title": "Design"},
}
WATCHERS = {
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-1/watchers",
    "isWatching": False,
    "watchCount": 1,
    "watchers": [{"displayName": "Ada"}],
}
WATCHERS_2 = {
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-2/watchers",
    "isWatching": False,
    "watchCount": 0,
    "watchers": [],
}
VOTES = {
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-1/votes",
    "votes": 2,
    "hasVoted": False,
    "voters": [],
}
PROP = {
    "key": "triage",
    "value": {"note": "kept"},
    "self": f"{ORIGIN}/rest/api/3/issue/EMBER-1/properties/triage",
}
PROJECT = {
    "id": "10000",
    "key": "EMBER",
    "name": "Ember",
    "projectTypeKey": "software",
    "self": f"{ORIGIN}/rest/api/3/project/10000",
}
COMPONENT = {"id": "10", "name": "API", "self": f"{ORIGIN}/rest/api/3/component/10"}
VERSION = {
    "id": "20",
    "name": "1.0",
    "released": False,
    "self": f"{ORIGIN}/rest/api/3/version/20",
}
STATUSES = {
    "id": "10004",
    "name": "Story",
    "self": f"{ORIGIN}/rest/api/3/issuetype/10004",
    "statuses": [{"name": "To Do"}],
}
PROJECT_PROP = {
    "key": "intake",
    "value": {"on": True},
    "self": f"{ORIGIN}/rest/api/3/project/10000/properties/intake",
}
ISSUE_1 = {
    "id": "10001",
    "key": "EMBER-1",
    "self": f"{ORIGIN}/rest/api/3/issue/10001",
    "fields": {
        "summary": "Login redirect",
        "description": ADF,
        "status": {"name": "In Progress"},
        "project": {"key": "EMBER"},
        "comment": {"total": 2, "comments": []},
        "worklog": {"total": 1, "worklogs": []},
        "attachment": [{"id": "55", "filename": "shot.png"}],
        "issuelinks": [LINK],
    },
}
ISSUE_2 = {
    "id": "10002",
    "key": "EMBER-2",
    "self": f"{ORIGIN}/rest/api/3/issue/10002",
    "fields": {
        "summary": "Empty notes",
        "description": None,
        "status": {"name": "To Do"},
        "project": {"key": "EMBER"},
        "comment": {"total": 0, "comments": []},
        "worklog": {"total": 0, "worklogs": []},
        "attachment": [],
        "issuelinks": [],
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

    def test_summary_counts_keys_and_skips_description_text(self):
        envelopes = [
            {"type": "jira", "body": ISSUE_1},
            {"type": "jira", "body": ISSUE_2},
            {"type": "jira", "body": COMMENT_A},
            {"type": "jira", "body": ATTACHMENT},
        ]
        text = pull_test.render_summary("EMBER", envelopes)
        self.assertIn("# EMBER", text)
        self.assertIn("## Issues (2)", text)
        self.assertIn("- EMBER-1", text)
        self.assertIn("- EMBER-2", text)
        self.assertIn("## Comments (1)", text)
        self.assertIn("- 100", text)
        self.assertIn("## Attachments (1)", text)
        self.assertIn("- shot.png", text)
        self.assertNotIn("First note", text)
        self.assertNotIn("Repro:", text)
        self.assertNotIn("attachment/content", text)

    def test_sidecar_path_sits_beside_jsonl(self):
        self.assertEqual(
            pull_test.readable_sidecar(Path("/tmp/intake.jsonl")),
            Path("/tmp/intake.jsonl.readable.md"),
        )


def _json(payload, status: int = 200):
    return pull_test.Response(status, {}, json.dumps(payload).encode())


class PullFlowTest(unittest.TestCase):
    def exchange(self, method, url, headers, body):
        self.calls.append((method, url, headers.get("Authorization"), body))
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertNotIn(TOKEN, headers["Authorization"])
        path, _, query = url.partition("?")
        path = path.rstrip("/")
        if path.endswith("/rest/api/3/search/jql"):
            self.assertEqual(method, "POST")
            payload = json.loads(body)
            self.assertEqual(payload["jql"], "project = EMBER ORDER BY key ASC")
            self.assertEqual(payload["fields"], ["key"])
            self.assertEqual(payload["maxResults"], 100)
            if "nextPageToken" not in payload:
                return _json(
                    {
                        "issues": [{"id": "10001", "key": "EMBER-1"}],
                        "nextPageToken": "page-2",
                        "isLast": False,
                    }
                )
            self.assertEqual(payload["nextPageToken"], "page-2")
            return _json({"issues": [{"id": "10002", "key": "EMBER-2"}], "isLast": True})
        if path.endswith("/project/EMBER/components"):
            return _json([COMPONENT])
        if path.endswith("/project/EMBER/statuses"):
            return _json([STATUSES])
        if path.endswith("/project/EMBER/properties/intake"):
            return _json(PROJECT_PROP)
        if path.endswith("/project/EMBER/properties"):
            return _json({"keys": [{"key": "intake"}]})
        if path.endswith("/project/EMBER/version"):
            return _json(
                {"startAt": 0, "maxResults": 100, "total": 1, "isLast": True, "values": [VERSION]}
            )
        if path.endswith("/project/EMBER"):
            return _json(PROJECT)
        if "/attachment/content/" in path or "/attachment/thumbnail/" in path:
            raise AssertionError(url)
        if path.endswith("/attachment/55"):
            return _json(ATTACHMENT)
        if path.endswith("/issue/EMBER-1/comment"):
            if "startAt=0" in query:
                return _json(
                    {
                        "startAt": 0,
                        "maxResults": 1,
                        "total": 2,
                        "isLast": False,
                        "comments": [COMMENT_A],
                    }
                )
            if "startAt=1" in query:
                return _json(
                    {
                        "startAt": 1,
                        "maxResults": 100,
                        "total": 2,
                        "isLast": True,
                        "comments": [COMMENT_B],
                    }
                )
            raise AssertionError(query)
        if path.endswith("/issue/EMBER-2/comment") or path.endswith("/issue/EMBER-2/worklog"):
            raise AssertionError(url)
        if path.endswith("/issue/EMBER-1/changelog"):
            return _json(CHANGELOG)
        if path.endswith("/issue/EMBER-2/changelog"):
            return _json(CHANGELOG_2)
        if path.endswith("/issue/EMBER-1/worklog"):
            return _json(
                {
                    "startAt": 0,
                    "maxResults": 100,
                    "total": 1,
                    "isLast": True,
                    "worklogs": [WORKLOG],
                }
            )
        if path.endswith("/issue/EMBER-1/remotelink"):
            return _json([REMOTE])
        if path.endswith("/issue/EMBER-2/remotelink"):
            return _json([])
        if path.endswith("/issue/EMBER-1/watchers"):
            return _json(WATCHERS)
        if path.endswith("/issue/EMBER-2/watchers"):
            return _json(WATCHERS_2)
        if path.endswith("/issue/EMBER-1/votes"):
            return _json(VOTES)
        if path.endswith("/issue/EMBER-2/votes"):
            return pull_test.Response(404, {}, b'{"errorMessages":["Voting is disabled"]}')
        if path.endswith("/issue/EMBER-1/properties/triage"):
            return _json(PROP)
        if path.endswith("/issue/EMBER-1/properties"):
            return _json({"keys": [{"key": "triage"}]})
        if path.endswith("/issue/EMBER-2/properties"):
            return _json({"keys": []})
        if path.endswith("/issue/EMBER-1"):
            self.assertIn("fields=%2Aall", query)
            return _json(ISSUE_1)
        if path.endswith("/issue/EMBER-2"):
            self.assertIn("fields=%2Aall", query)
            return _json(ISSUE_2)
        raise AssertionError(url)

    def test_full_project_becomes_one_envelope_per_object(self):
        self.calls = []
        envelopes = list(pull_test.pull(ORIGIN, EMAIL, TOKEN, "EMBER", self.exchange))
        bodies = [item["body"] for item in envelopes]
        self.assertEqual(
            bodies,
            [
                PROJECT,
                COMPONENT,
                VERSION,
                STATUSES,
                PROJECT_PROP,
                ISSUE_1,
                LINK,
                COMMENT_A,
                COMMENT_B,
                CHANGELOG,
                WORKLOG,
                ATTACHMENT,
                REMOTE,
                WATCHERS,
                VOTES,
                PROP,
                ISSUE_2,
                CHANGELOG_2,
                WATCHERS_2,
            ],
        )
        self.assertEqual(bodies[5]["fields"]["description"], ADF)
        self.assertIsNone(bodies[16]["fields"]["description"])
        self.assertEqual(bodies[8]["body"], ADF)
        urls = [url for _method, url, _auth, _body in self.calls]
        self.assertEqual(urls[0], f"{ORIGIN}/rest/api/3/search/jql")
        self.assertTrue(any("startAt=1" in url and "/comment" in url for url in urls))
        self.assertFalse(any("/issue/EMBER-2/comment" in url or "/issue/EMBER-2/worklog" in url for url in urls))
        self.assertFalse(any("/attachment/content/" in url or "/attachment/thumbnail/" in url for url in urls))
        for envelope in envelopes:
            self.assertEqual(set(envelope), {"type", "body"})
            self.assertEqual(envelope["type"], "jira")
            assert_valid(envelope)
        text = pull_test.render_summary("EMBER", envelopes)
        self.assertIn("## Issues (2)", text)
        self.assertIn("## Comments (2)", text)
        self.assertIn("## Changelog pages (2)", text)
        self.assertIn("## Worklogs (1)", text)
        self.assertIn("## Attachments (1)", text)
        self.assertIn("- shot.png", text)
        self.assertNotIn("First note", text)
        self.assertNotIn("Repro:", text)
        self.assertNotIn("Did the work", text)

    def test_detail_waves_use_the_worker_cap(self):
        seen = []
        real = pull_test.map_ordered

        def wrapped(fn, items, workers):
            seen.append(workers)
            return real(fn, items, workers)

        pull_test.map_ordered = wrapped
        try:
            self.calls = []
            list(pull_test.pull(ORIGIN, EMAIL, TOKEN, "EMBER", self.exchange, workers=5))
        finally:
            pull_test.map_ordered = real
        self.assertTrue(seen)
        self.assertTrue(all(workers == 5 for workers in seen))

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

    def test_default_concurrency_is_thirty_two(self):
        prior = os.environ.pop("JIRA_PULL_CONCURRENCY", None)
        try:
            self.assertEqual(pull_test.DEFAULT_CONCURRENCY, 32)
            self.assertEqual(pull_test.MAX_CONCURRENCY, 64)
            self.assertEqual(pull_test.resolve_concurrency(None), 32)
            self.assertEqual(pull_test.resolve_concurrency(8), 8)
            with self.assertRaises(pull_test.PullError):
                pull_test.resolve_concurrency(65)
        finally:
            if prior is not None:
                os.environ["JIRA_PULL_CONCURRENCY"] = prior

    def test_throttle_without_retry_after_backs_off(self):
        calls = {"n": 0}

        def exchange(method, url, headers, body):
            calls["n"] += 1
            if calls["n"] < 3:
                return pull_test.Response(429, {}, b"slow down")
            return pull_test.Response(200, {}, b'{"ok":true}')

        waits: list[float] = []
        response = pull_test.exchange_with_retry(
            exchange,
            "GET",
            f"{ORIGIN}/rest/api/3/issue/EMBER-1/comment",
            {},
            None,
            sleep=waits.append,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(waits, [1.0, 2.0])

    def test_remaining_pages_follow_reported_page_size(self):
        page = {"startAt": 0, "maxResults": 1, "total": 2, "isLast": False, "comments": []}
        self.assertEqual(pull_test.remaining_starts(page, "https://example.test/comment"), [1])
        self.assertEqual(pull_test.remaining_starts({**page, "isLast": True}, "https://example.test/comment"), [])

    def test_refuses_attachment_bytes(self):
        def exchange(method, url, headers, body):
            raise AssertionError(url)

        with self.assertRaises(pull_test.PullError):
            pull_test.fetch_json(f"{ORIGIN}/rest/api/3/attachment/content/55", EMAIL, TOKEN, exchange)
        with self.assertRaises(pull_test.PullError):
            pull_test.fetch_json(f"{ORIGIN}/rest/api/3/attachment/thumbnail/55", EMAIL, TOKEN, exchange)

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
