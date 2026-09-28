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


class ReadableOutputTest(unittest.TestCase):
    def test_jsonl_stays_one_object_per_line(self):
        import io

        envelopes = [
            {"type": "github", "body": {"number": 12, "title": "Login redirect", "state": "open"}},
        ]
        buffer = io.StringIO()
        self.assertEqual(pull_test.emit_jsonl(envelopes, buffer), 1)
        line = buffer.getvalue()
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line), envelopes[0])
        assert_valid(envelopes[0])

    def test_summary_groups_issue_pull_and_commit(self):
        records = [
            ("issue", {"number": 12, "title": "Login redirect", "state": "open"}),
            ("issue", {"number": 3, "title": "Add parser", "state": "open", "pull_request": {"url": "x"}}),
            (
                "pull_request",
                {"number": 3, "title": "Add parser", "state": "closed", "merged_at": "2026-09-01T00:00:00Z"},
            ),
            ("commit", {"sha": "abcdef1234567890", "commit": {"message": "init\n\nmore"}}),
            ("commit", {"sha": "bbbbbbb1234567890", "commit": {"message": "wip"}}),
        ]
        text = pull_test.render_summary("acme/widget", records, duplicate_commits=2)
        self.assertIn("# acme/widget", text)
        self.assertIn("Envelopes: 5.", text)
        self.assertIn("Unique commits: 2. Duplicate SHA hits omitted: 2.", text)
        self.assertIn("| Issues | 2 |", text)
        self.assertIn("| Pull requests | 1 |", text)
        self.assertIn("| Commits | 2 |", text)
        issues = text.split("## Issues", 1)[1].split("## ", 1)[0]
        pulls = text.split("## Pull requests", 1)[1].split("## ", 1)[0]
        commits = text.split("## Commits", 1)[1].split("## ", 1)[0]
        self.assertIn("- #12 open — Login redirect", issues)
        self.assertIn("- #3 open — Add parser", issues)
        self.assertIn("- #3 merged — Add parser", pulls)
        self.assertEqual(pulls.count("#3"), 1)
        self.assertIn("- `abcdef1` init", commits)
        self.assertNotIn("more", commits)
        self.assertIn("- (none)", text.split("## Labels", 1)[1].split("## ", 1)[0])

    def test_summary_excerpt_uses_description_body(self):
        records = [
            (
                "issue",
                {
                    "number": 12,
                    "title": "Login redirect",
                    "state": "open",
                    "body": "Repro: sign in with ?next=/settings\n\nand land on /.",
                },
            )
        ]
        text = pull_test.render_summary("acme/widget", records)
        self.assertIn("- #12 open — Login redirect — Repro: sign in with ?next=/settings and land on /.", text)

    def test_comment_lines_lead_with_author_and_body(self):
        long_body = "Please keep the query string when the session is created. " * 4
        comment = {
            "id": 9,
            "body": long_body,
            "user": {"login": "octocat"},
            "issue_url": "https://api.github.com/repos/acme/widget/issues/12",
        }
        review_comment = {
            "id": 8,
            "body": "nit on the parser",
            "user": {"login": "hubot"},
            "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/3",
        }
        review = {
            "id": 4,
            "body": "LGTM",
            "state": "APPROVED",
            "user": {"login": "octocat"},
            "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/3",
        }
        records = [
            ("issue", {"id": 100, "number": 12, "title": "Login redirect", "state": "open", "body": long_body}),
            ("pull_request", {"id": 200, "number": 3, "title": "Add parser", "state": "open", "body": "What this changes."}),
            ("issue_comment", comment),
            ("pull_request_review_comment", review_comment),
            ("pull_request_review", review),
            ("commit", {"sha": "abcdef1234567890", "commit": {"message": ""}, "author": {"login": "octocat"}}),
        ]
        text = pull_test.render_summary("acme/widget", records)
        issues = text.split("## Issues", 1)[1].split("## ", 1)[0]
        pulls = text.split("## Pull requests", 1)[1].split("## ", 1)[0]
        comments = text.split("## Issue comments", 1)[1].split("## ", 1)[0]
        review_comments = text.split("## Pull request review comments", 1)[1].split("## ", 1)[0]
        reviews = text.split("## Pull request reviews", 1)[1].split("## ", 1)[0]
        commits = text.split("## Commits", 1)[1].split("## ", 1)[0]
        self.assertIn("- #12 open — Login redirect — Please keep the query string", issues)
        self.assertIn("…", issues)
        self.assertNotIn(long_body, issues)
        self.assertIn("- #3 open — Add parser — What this changes.", pulls)
        self.assertIn("- octocat on #12 — Please keep the query string", comments)
        self.assertIn("…", comments)
        self.assertNotIn("comment 9", comments)
        self.assertNotIn(long_body, comments)
        self.assertIn("- hubot on #3 — nit on the parser", review_comments)
        self.assertNotIn("comment 8", review_comments)
        self.assertIn("- octocat on #3 APPROVED — LGTM", reviews)
        self.assertNotIn("review 4", reviews)
        self.assertIn("- `abcdef1` octocat", commits)
        self.assertEqual(comment["id"], 9)
        self.assertEqual(comment["body"], long_body)
        envelope = pull_test.wrap_github(comment)
        self.assertEqual(set(envelope), {"type", "body"})
        self.assertEqual(envelope["body"], comment)

    def test_sidecar_path_sits_beside_jsonl(self):
        self.assertEqual(
            pull_test.readable_sidecar(Path("/tmp/intake.jsonl")),
            Path("/tmp/intake.jsonl.readable.md"),
        )


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


def ok(payload, headers=None) -> pull_test.Response:
    return pull_test.Response(200, headers or {}, json.dumps(payload).encode())


class PullFlowTest(unittest.TestCase):
    def exchange(self, method, url, headers, body):
        with self._lock:
            self.calls.append((method, url, headers.get("Authorization"), body))
        return self.route(url)

    def route(self, url: str) -> pull_test.Response:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        repo = "/repos/acme/widget"
        if path == f"{repo}/installation":
            return ok({"id": 9})
        if path == "/app/installations/9/access_tokens":
            return ok({"token": "ghs_test"})
        if path == repo:
            return ok({"full_name": "acme/widget", "default_branch": "main", "private": False})
        if path == f"{repo}/issues/comments":
            return ok(
                [
                    {
                        "id": 9,
                        "body": "thanks",
                        "user": {"login": "octocat"},
                        "issue_url": "https://api.github.com/repos/acme/widget/issues/1",
                    }
                ]
            )
        if path == f"{repo}/issues/events":
            return ok([{"id": 5, "event": "closed"}])
        if path == f"{repo}/issues":
            return ok(
                [
                    {"number": 1, "title": "bug"},
                    {"number": 2, "title": "pr", "pull_request": {"url": "https://api.github.com/repos/acme/widget/pulls/2"}},
                ]
            )
        if path == f"{repo}/issues/1":
            return ok({"number": 1, "title": "bug", "state": "open", "body": "Steps to reproduce."})
        if path == f"{repo}/issues/2":
            return ok(
                {
                    "number": 2,
                    "title": "pr",
                    "state": "open",
                    "body": "Issue side of the pull request.",
                    "pull_request": {"url": "https://api.github.com/repos/acme/widget/pulls/2"},
                }
            )
        if path == f"{repo}/pulls/comments":
            return ok(
                [
                    {
                        "id": 8,
                        "body": "nit",
                        "user": {"login": "octocat"},
                        "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/2",
                    }
                ]
            )
        if path == f"{repo}/pulls/2/reviews":
            return ok(
                [
                    {
                        "id": 4,
                        "body": "LGTM",
                        "state": "APPROVED",
                        "user": {"login": "octocat"},
                        "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/2",
                    }
                ]
            )
        if path == f"{repo}/pulls/2/commits":
            return ok(
                [
                    {"sha": "ccc", "commit": {"message": "from fork"}},
                    {"sha": "aaa", "commit": {"message": "init"}},
                ]
            )
        if path == f"{repo}/pulls/2":
            return ok(
                {
                    "number": 2,
                    "title": "pr",
                    "state": "closed",
                    "merged_at": "2026-09-01T00:00:00Z",
                    "body": "What this changes.",
                }
            )
        if path == f"{repo}/pulls":
            return ok([{"number": 2, "title": "pr"}])
        if path == f"{repo}/branches":
            return ok(
                [
                    {"name": "feature", "commit": {"sha": "bbb"}},
                    {"name": "main", "commit": {"sha": "aaa"}},
                ]
            )
        if path == f"{repo}/commits":
            sha = query.get("sha", [""])[0]
            page = query.get("page", ["1"])[0]
            if sha == "main" and page == "1":
                link = '<https://api.github.com/repos/acme/widget/commits?sha=main&per_page=100&page=2>; rel="next"'
                return ok([{"sha": "aaa", "commit": {"message": "init\n\nmore"}}], {"Link": link})
            if sha == "main" and page == "2":
                return ok([{"sha": "old", "commit": {"message": "root"}}])
            if sha == "feature":
                return ok(
                    [
                        {"sha": "bbb", "commit": {"message": "wip"}},
                        {"sha": "aaa", "commit": {"message": "init"}},
                    ]
                )
        if path == f"{repo}/comments":
            return ok([{"id": 6, "body": "note", "commit_id": "aaa", "user": {"login": "octocat"}}])
        if path == f"{repo}/releases":
            return ok([{"id": 3, "tag_name": "v1.0.0", "name": "v1", "body": "notes"}])
        if path == f"{repo}/tags":
            return ok([{"name": "v1.0.0", "commit": {"sha": "aaa"}}])
        if path == f"{repo}/git/matching-refs/tags":
            return ok(
                [
                    {"ref": "refs/tags/v1.0.0", "object": {"sha": "ttt", "type": "tag"}},
                    {"ref": "refs/tags/light", "object": {"sha": "aaa", "type": "commit"}},
                ]
            )
        if path == f"{repo}/git/tags/ttt":
            return ok({"sha": "ttt", "tag": "v1.0.0", "message": "annotated\n\nbody", "object": {"sha": "aaa"}})
        if path == f"{repo}/labels":
            return ok([{"name": "bug"}])
        if path == f"{repo}/milestones":
            return ok([{"number": 1, "title": "m1", "state": "open"}])
        if path == f"{repo}/contributors":
            return ok([{"login": "octocat", "contributions": 3}])
        raise AssertionError(url)

    def test_full_rest_backfill_is_one_envelope_per_object(self):
        import threading

        self.calls = []
        self._lock = threading.Lock()
        capture = pull_test.pull(
            "acme/widget",
            "5075660",
            "unused.pem",
            self.exchange,
            now=1_700_000_000,
            sign=fake_sign,
            workers=4,
        )
        envelopes = capture.envelopes()
        kinds = [kind for kind, _body in capture.records]
        self.assertEqual(
            kinds,
            [
                "repository",
                "label",
                "milestone",
                "issue",
                "issue",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "branch",
                "branch",
                "commit",
                "commit",
                "commit",
                "commit",
                "commit_comment",
                "release",
                "tag",
                "annotated_tag",
                "issue_event",
                "contributor",
            ],
        )
        self.assertEqual(capture.duplicate_commits, 2)
        by_kind: dict[str, list] = {}
        for kind, body in capture.records:
            by_kind.setdefault(kind, []).append(body)
        self.assertEqual(by_kind["issue"][0]["body"], "Steps to reproduce.")
        self.assertEqual(by_kind["issue"][1]["body"], "Issue side of the pull request.")
        self.assertEqual(by_kind["pull_request"][0]["body"], "What this changes.")
        self.assertEqual(by_kind["pull_request"][0]["merged_at"], "2026-09-01T00:00:00Z")
        self.assertEqual([item["name"] for item in by_kind["branch"]], ["main", "feature"])
        self.assertEqual([item["sha"] for item in by_kind["commit"]], ["aaa", "old", "bbb", "ccc"])
        self.assertEqual(by_kind["annotated_tag"][0]["message"], "annotated\n\nbody")
        urls = [url.split("?", 1)[0] for _method, url, _auth, _body in self.calls]
        self.assertIn("https://api.github.com/repos/acme/widget/issues/1", urls)
        self.assertIn("https://api.github.com/repos/acme/widget/issues/2", urls)
        self.assertIn("https://api.github.com/repos/acme/widget/pulls/2", urls)
        self.assertIn("https://api.github.com/repos/acme/widget/pulls/2/reviews", urls)
        self.assertIn("https://api.github.com/repos/acme/widget/pulls/2/commits", urls)
        self.assertIn("https://api.github.com/repos/acme/widget/git/tags/ttt", urls)
        self.assertFalse(any(url.endswith("/git/tags/aaa") or url.endswith("/commits/aaa") for url in urls))
        self.assertTrue(any(url.endswith("/installation") for url in urls))
        for method, url, auth, req_body in self.calls:
            if url.endswith("/installation") or url.endswith("/access_tokens"):
                self.assertTrue(auth.startswith("Bearer ey"))
            else:
                self.assertEqual(auth, "Bearer ghs_test")
            if url.endswith("/access_tokens"):
                self.assertEqual(req_body, b"{}")
            self.assertEqual(method, "GET" if not url.endswith("/access_tokens") else "POST")
        for envelope in envelopes:
            self.assertEqual(set(envelope), {"type", "body"})
            self.assertEqual(envelope["type"], "github")
            assert_valid(envelope)
        text = pull_test.render_summary("acme/widget", capture.records, duplicate_commits=capture.duplicate_commits)
        self.assertIn("Unique commits: 4. Duplicate SHA hits omitted: 2.", text)
        self.assertIn("- octocat on #1 — thanks", text)
        self.assertIn("- octocat on #2 APPROVED — LGTM", text)
        self.assertIn("- octocat on #2 — nit", text)
        comment = by_kind["issue_comment"][0]
        self.assertEqual(comment["id"], 9)
        self.assertEqual(comment["body"], "thanks")
        self.assertEqual(pull_test.wrap_github(comment)["body"], comment)
        self.assertIn("- octocat (3)", text)
        self.assertIn("- v1.0.0 `ttt` — annotated", text)
        commits = text.split("## Commits", 1)[1].split("## ", 1)[0]
        self.assertEqual(commits.count("`aaa`"), 1)

    def test_missing_annotated_tags_are_empty(self):
        def exchange(method, url, headers, body):
            path = url.split("?", 1)[0]
            if path.endswith("/installation"):
                return ok({"id": 9})
            if path.endswith("/access_tokens"):
                return ok({"token": "ghs_test"})
            if path == "https://api.github.com/repos/acme/widget":
                return ok({"full_name": "acme/widget", "default_branch": "main"})
            if path.endswith("/git/matching-refs/tags"):
                return pull_test.Response(404, {}, b'{"message":"Not Found"}')
            return ok([])

        capture = pull_test.pull(
            "acme/widget",
            "1",
            "unused.pem",
            exchange,
            now=1,
            sign=fake_sign,
            workers=2,
        )
        self.assertEqual([kind for kind, _body in capture.records], ["repository"])
        self.assertEqual(capture.duplicate_commits, 0)
        assert_valid(capture.envelopes()[0])

    def test_branch_names_are_query_encoded(self):
        seen: list[str] = []

        def exchange(method, url, headers, body):
            seen.append(url)
            return ok([])

        client = pull_test.GithubPull("acme/widget", "ghs_test", exchange, workers=1)
        self.assertEqual(client.commits_for({"name": "feature/login"}), [])
        self.assertIn("sha=feature%2Flogin", seen[0])

    def test_page_cap_is_an_error(self):
        def exchange(method, url, headers, body):
            nxt = "https://api.github.com/repos/acme/widget/labels?page=2"
            link = f'<{nxt}>; rel="next"'
            return pull_test.Response(200, {"Link": link}, b"[{}]")

        client = pull_test.GithubPull("acme/widget", "ghs_test", exchange, workers=1)
        with self.assertRaises(pull_test.PullError) as caught:
            client.get_list("https://api.github.com/repos/acme/widget/labels?per_page=100")
        self.assertIn(str(pull_test.MAX_PAGES), str(caught.exception))

    def test_rejects_repo_without_owner(self):
        with self.assertRaises(pull_test.PullError):
            pull_test.pull(
                "photon",
                "1",
                "unused.pem",
                self.exchange,
                now=1,
                sign=fake_sign,
            )


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_results_stay_in_list_order(self):
        import time

        def work(number: int) -> int:
            time.sleep(0.02 if number == 1 else 0)
            return number

        self.assertEqual(pull_test.map_ordered(work, [1, 2, 3, 4], 4), [1, 2, 3, 4])

    def test_secondary_rate_limit_without_retry_after_still_waits(self):
        body = b'{"message":"You have exceeded a secondary rate limit. Please wait a few minutes."}'
        response = pull_test.Response(403, {}, body)
        self.assertEqual(pull_test.retry_after_seconds(response), pull_test.SECONDARY_RATE_LIMIT_WAIT)

    def test_permission_403_is_not_a_rate_limit(self):
        body = b'{"message":"Resource not accessible by integration"}'
        response = pull_test.Response(403, {}, body)
        self.assertIsNone(pull_test.retry_after_seconds(response))

    def test_shared_gate_holds_a_later_caller(self):
        clock = {"now": 0.0}
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        gate = pull_test.RateLimitGate(clock=lambda: clock["now"], sleep=sleep)
        gate.extend(5)
        gate.wait()
        self.assertEqual(sleeps, [5.0])
        gate.wait()
        self.assertEqual(sleeps, [5.0])

    def test_retry_after_is_waited_then_the_body_is_kept(self):
        calls = {"n": 0}

        def exchange(method, url, headers, body):
            calls["n"] += 1
            if calls["n"] == 1:
                return pull_test.Response(429, {"Retry-After": "2"}, b"slow down")
            return pull_test.Response(200, {}, b'{"body":"kept"}')

        waits: list[float] = []
        response = pull_test.exchange_with_retry(
            exchange, "GET", "https://api.github.com/repos/acme/widget/issues/1", {}, None, sleep=waits.append
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(waits, [2.0])
        self.assertEqual(response.json()["body"], "kept")

    def test_default_concurrency_is_thirty_two(self):
        import os

        prior = os.environ.pop("GITHUB_PULL_CONCURRENCY", None)
        try:
            self.assertEqual(pull_test.DEFAULT_CONCURRENCY, 32)
            self.assertEqual(pull_test.MAX_CONCURRENCY, 80)
            self.assertEqual(pull_test.resolve_concurrency(None), 32)
            self.assertEqual(pull_test.resolve_concurrency(8), 8)
            with self.assertRaises(pull_test.PullError):
                pull_test.resolve_concurrency(0)
            with self.assertRaises(pull_test.PullError):
                pull_test.resolve_concurrency(81)
        finally:
            if prior is not None:
                os.environ["GITHUB_PULL_CONCURRENCY"] = prior


if __name__ == "__main__":
    unittest.main()
