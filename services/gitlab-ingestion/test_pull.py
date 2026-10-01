"""pull.py against a mocked GitLab. No network."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pull  # noqa: E402
from pull import GitlabPull, PullError, RateLimitGate, Response  # noqa: E402
from validate import assert_valid  # noqa: E402

BASE = "https://gitlab.example.com"
API = BASE + "/api/v4"
TOKEN = "glpat-test-token-never-printed"
PROJECT = {"id": 42, "path_with_namespace": "acme/widget", "description": "Widget service."}


def mr(iid: int, updated: str = "2026-09-20T00:00:00Z", **fields) -> dict:
    body = {
        "id": 700 + iid,
        "iid": iid,
        "project_id": 42,
        "title": f"MR {iid}",
        "description": f"Why MR {iid} exists.",
        "state": "merged",
        "updated_at": updated,
        "author": {"username": "pat"},
    }
    body.update(fields)
    return body


def issue(iid: int, updated: str = "2026-09-20T00:00:00Z", **fields) -> dict:
    body = {
        "id": 500 + iid,
        "iid": iid,
        "project_id": 42,
        "title": f"Issue {iid}",
        "description": "Ticket context.",
        "state": "opened",
        "updated_at": updated,
        "confidential": False,
        "author": {"username": "sam"},
    }
    body.update(fields)
    return body


def note(note_id: int, noteable_type: str, iid: int, text: str, **fields) -> dict:
    body = {
        "id": note_id,
        "body": text,
        "noteable_type": noteable_type,
        "noteable_iid": iid,
        "system": False,
        "internal": False,
        "author": {"username": "pat"},
    }
    body.update(fields)
    return body


class FakeClock:
    """Wall and monotonic time together. sleep() advances both."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeGitLab:
    """Routes GET paths to lists (paged by ``page_size``) or objects.

    ``script`` maps a path to responses served before the real one, for
    throttles. ``headers`` adds headers to every real response.
    """

    def __init__(self, routes: dict[str, object], *, page_size: int = 2, headers: dict | None = None) -> None:
        self.routes = routes
        self.page_size = page_size
        self.headers = headers or {}
        self.script: dict[str, list[Response]] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str]) -> Response:
        assert method == "GET", method
        self.calls.append((url, headers))
        parts = urlsplit(url)
        assert url.startswith(API + "/"), url
        path = parts.path[len("/api/v4") :]
        queued = self.script.get(path)
        if queued:
            return queued.pop(0)
        if path not in self.routes:
            return Response(404, {}, b'{"message":"404 Not Found"}')
        value = self.routes[path]
        if isinstance(value, Response):
            return value
        if isinstance(value, dict):
            return Response(200, dict(self.headers), json.dumps(value).encode("utf-8"))
        query = parse_qs(parts.query)
        page = int(query.get("page", ["1"])[0])
        start = (page - 1) * self.page_size
        chunk = value[start : start + self.page_size]
        headers = dict(self.headers)
        if start + self.page_size < len(value):
            following = dict(query)
            following["page"] = [str(page + 1)]
            next_query = "&".join(f"{key}={values[0]}" for key, values in following.items())
            headers["Link"] = f'<{API}{path}?{next_query}>; rel="next", <{API}{path}?page=1>; rel="first"'
        return Response(200, headers, json.dumps(chunk).encode("utf-8"))

    def paths(self) -> list[str]:
        return [urlsplit(url).path[len("/api/v4") :] for url, _ in self.calls]

    def queries(self, path: str) -> list[dict[str, list[str]]]:
        return [parse_qs(urlsplit(url).query) for url, _ in self.calls if urlsplit(url).path == "/api/v4" + path]


def default_routes() -> dict[str, object]:
    return {
        "/projects/acme%2Fwidget": PROJECT,
        "/projects/42": PROJECT,
        "/projects/42/merge_requests": [mr(1), mr(2), mr(3, state="opened")],
        "/projects/42/issues": [issue(10), issue(11, confidential=True), issue(12, state="closed")],
        "/projects/42/merge_requests/1/notes": [
            note(1, "MergeRequest", 1, "Decision: keep the intake raw."),
            note(2, "MergeRequest", 1, "approved this merge request", system=True),
            note(3, "MergeRequest", 1, "Internal only.", internal=True),
        ],
        "/projects/42/merge_requests/2/notes": [],
        "/projects/42/merge_requests/3/notes": [note(4, "MergeRequest", 3, "Looks good.")],
        "/projects/42/issues/10/notes": [note(5, "Issue", 10, "Repro steps attached.")],
        "/projects/42/issues/11/notes": [note(6, "Issue", 11, "Confidential detail.")],
        "/projects/42/issues/12/notes": [],
    }


def run_pull(fake: FakeGitLab, *, cutoff: str | None = None, workers: int = 4, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    gate = RateLimitGate(clock=clock.time, sleep=clock.sleep)
    puller = GitlabPull(BASE, "acme/widget", TOKEN, fake, workers, gate=gate, sleep=clock.sleep, clock=clock.time)
    return puller.capture(cutoff), clock


class BackfillTest(unittest.TestCase):
    def test_backfill_order_and_one_envelope_per_object(self):
        fake = FakeGitLab(default_routes())
        capture, _ = run_pull(fake)
        kinds = [kind for kind, _ in capture.records]
        self.assertEqual(
            kinds,
            ["project"]
            + ["merge_request"] * 3
            + ["merge_request_note"] * 3
            + ["issue"] * 2
            + ["issue_note"],
        )
        envelopes = capture.envelopes()
        for line in envelopes:
            assert_valid(line)
            self.assertEqual(set(line), {"type", "body"})
            self.assertEqual(line["type"], "gitlab")
        self.assertEqual(envelopes[0]["body"], PROJECT)
        self.assertEqual(envelopes[1]["body"], mr(1))

    def test_bodies_are_unchanged_raw_objects(self):
        routes = default_routes()
        capture, _ = run_pull(FakeGitLab(routes))
        bodies = [body for _, body in capture.records]
        self.assertIn(routes["/projects/42/merge_requests/1/notes"][1], bodies)
        self.assertIn(routes["/projects/42/issues"][2], bodies)

    def test_confidential_issues_and_internal_notes_are_skipped_and_counted(self):
        fake = FakeGitLab(default_routes())
        capture, _ = run_pull(fake)
        bodies = [body for _, body in capture.records]
        self.assertNotIn(issue(11, confidential=True), bodies)
        self.assertFalse(any(body.get("body") == "Internal only." for body in bodies))
        self.assertFalse(any(body.get("body") == "Confidential detail." for body in bodies))
        self.assertNotIn("/projects/42/issues/11/notes", fake.paths())
        self.assertEqual(capture.skipped, {"confidential_issues": 1, "internal_notes": 1})

    def test_pagination_follows_link_headers(self):
        routes = default_routes()
        routes["/projects/42/merge_requests"] = [mr(iid) for iid in range(1, 6)]
        for iid in range(4, 6):
            routes[f"/projects/42/merge_requests/{iid}/notes"] = []
        fake = FakeGitLab(routes, page_size=2)
        capture, _ = run_pull(fake)
        self.assertEqual([body["iid"] for kind, body in capture.records if kind == "merge_request"], [1, 2, 3, 4, 5])
        self.assertEqual([query.get("page", ["1"])[0] for query in fake.queries("/projects/42/merge_requests")], ["1", "2", "3"])

    def test_list_requests_ask_for_everything_in_a_stable_order(self):
        fake = FakeGitLab(default_routes())
        run_pull(fake)
        mrs = fake.queries("/projects/42/merge_requests")[0]
        self.assertEqual(mrs["state"], ["all"])
        self.assertEqual(mrs["order_by"], ["created_at"])
        self.assertEqual(mrs["sort"], ["asc"])
        self.assertEqual(mrs["per_page"], ["100"])
        self.assertNotIn("updated_after", mrs)
        issues = fake.queries("/projects/42/issues")[0]
        self.assertEqual(issues["scope"], ["all"])
        self.assertEqual(issues["state"], ["all"])
        self.assertEqual(issues["order_by"], ["created_at"])

    def test_token_goes_in_private_token_header_only(self):
        fake = FakeGitLab(default_routes())
        capture, _ = run_pull(fake)
        for url, headers in fake.calls:
            self.assertEqual(headers["PRIVATE-TOKEN"], TOKEN)
            self.assertNotIn(TOKEN, url)
        self.assertNotIn(TOKEN, json.dumps(capture.envelopes()))
        self.assertNotIn(TOKEN, pull.render_summary("acme/widget", capture, None))

    def test_pagination_link_off_the_instance_is_refused(self):
        routes = default_routes()
        routes["/projects/42/merge_requests"] = Response(
            200, {"Link": '<https://evil.example.com/api/v4/steal?page=2>; rel="next"'}, b"[]"
        )
        with self.assertRaisesRegex(PullError, "refusing pagination link"):
            run_pull(FakeGitLab(routes))

    def test_page_cap_stops_with_an_error(self):
        routes = default_routes()
        routes["/projects/42/merge_requests"] = [mr(iid) for iid in range(1, 8)]
        with mock.patch.object(pull, "MAX_PAGES", 3):
            with self.assertRaisesRegex(PullError, "stopped after 3 pages"):
                run_pull(FakeGitLab(routes, page_size=2))

    def test_numeric_project_id_is_accepted(self):
        fake = FakeGitLab(default_routes())
        clock = FakeClock()
        gate = RateLimitGate(clock=clock.time, sleep=clock.sleep)
        GitlabPull(BASE, "42", TOKEN, fake, 2, gate=gate, sleep=clock.sleep, clock=clock.time).capture()
        self.assertEqual(fake.paths()[0], "/projects/42")

    def test_auth_and_missing_project_errors_are_clear(self):
        for status, expected in ((401, "read_api"), (403, "read_api"), (404, "GITLAB_PROJECT")):
            with self.subTest(status=status):
                routes = default_routes()
                routes["/projects/acme%2Fwidget"] = Response(status, {}, b'{"message":"no"}')
                with self.assertRaisesRegex(PullError, expected):
                    run_pull(FakeGitLab(routes))


class ReconciliationTest(unittest.TestCase):
    def test_cutoff_limits_lists_and_note_fetches(self):
        routes = default_routes()
        routes["/projects/42/merge_requests"] = [mr(3, updated="2026-10-01T05:00:00Z", state="opened")]
        routes["/projects/42/issues"] = [issue(12, updated="2026-10-01T06:00:00Z")]
        fake = FakeGitLab(routes)
        capture, _ = run_pull(fake, cutoff="2026-10-01T00:00:00Z")
        for path in ("/projects/42/merge_requests", "/projects/42/issues"):
            self.assertEqual(fake.queries(path)[0]["updated_after"], ["2026-10-01T00:00:00Z"])
        self.assertNotIn("/projects/42/merge_requests/1/notes", fake.paths())
        self.assertIn("/projects/42/merge_requests/3/notes", fake.paths())
        self.assertEqual([kind for kind, _ in capture.records], ["project", "merge_request", "merge_request_note", "issue"])

    def test_resolve_cutoff(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(pull.resolve_cutoff(None, None, now=now))
        self.assertEqual(pull.resolve_cutoff(None, 24, now=now), "2026-09-30T12:00:00Z")
        self.assertEqual(pull.resolve_cutoff("2026-10-01T00:00:00Z", None), "2026-10-01T00:00:00Z")
        self.assertEqual(pull.resolve_cutoff("2026-09-30T17:00:00-07:00", None), "2026-10-01T00:00:00Z")
        for args in (("2026-10-01T00:00:00", None), ("yesterday", None), (None, 0), (None, -1), ("2026-10-01T00:00:00Z", 1)):
            with self.subTest(args=args):
                with self.assertRaises(PullError):
                    pull.resolve_cutoff(*args, now=now)


class RateLimitTest(unittest.TestCase):
    def test_429_with_retry_after_waits_then_retries(self):
        fake = FakeGitLab(default_routes())
        fake.script["/projects/42/merge_requests"] = [Response(429, {"Retry-After": "7"}, b"{}")]
        with redirect_stderr(io.StringIO()):
            capture, clock = run_pull(fake)
        self.assertIn(7.0, clock.slept)
        self.assertEqual(sum(1 for kind, _ in capture.records if kind == "merge_request"), 3)

    def test_429_without_retry_after_waits_until_reset(self):
        clock = FakeClock()
        fake = FakeGitLab(default_routes())
        fake.script["/projects/42/issues"] = [
            Response(429, {"RateLimit-Remaining": "0", "RateLimit-Reset": str(int(clock.now) + 30)}, b"{}")
        ]
        with redirect_stderr(io.StringIO()):
            run_pull(fake, clock=clock)
        self.assertIn(30.0, clock.slept)

    def test_server_errors_back_off_exponentially_then_give_up(self):
        fake = FakeGitLab(default_routes())
        fake.script["/projects/42/merge_requests"] = [Response(503, {}, b"busy") for _ in range(5)]
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(PullError, r"\(503\)"):
                run_pull(fake)

    def test_network_errors_are_retryable(self):
        for error in (urllib.error.URLError("dns failure"), TimeoutError("timed out"), ConnectionResetError("reset")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(pull.urllib.request, "urlopen", side_effect=error):
                    response = pull.urllib_exchange("GET", API + "/projects/42", {})
                self.assertEqual(response.status, 503)
                self.assertIn(b"network error", response.body)
                self.assertEqual(pull.retry_after_seconds(response), 1.0)

    def test_backoff_schedule(self):
        response = Response(502, {}, b"")
        self.assertEqual([pull.retry_after_seconds(response, attempt) for attempt in range(4)], [1.0, 2.0, 4.0, 8.0])
        self.assertIsNone(pull.retry_after_seconds(Response(404, {}, b"")))
        self.assertIsNone(pull.retry_after_seconds(Response(200, {"Retry-After": "5"}, b"")))

    def test_waits_are_capped(self):
        fake = FakeGitLab(default_routes())
        fake.script["/projects/42/merge_requests"] = [Response(429, {"Retry-After": "9999"}, b"{}")]
        with redirect_stderr(io.StringIO()):
            _, clock = run_pull(fake)
        self.assertLessEqual(max(clock.slept), pull.MAX_RETRY_WAIT)

    def test_low_quota_pauses_every_worker_until_reset(self):
        clock = FakeClock()
        fake = FakeGitLab(
            default_routes(),
            headers={"RateLimit-Limit": "2000", "RateLimit-Remaining": "10", "RateLimit-Reset": str(int(clock.now) + 20)},
        )
        with redirect_stderr(io.StringIO()) as err:
            run_pull(fake, clock=clock, workers=1)
        self.assertIn(20.0, clock.slept)
        self.assertIn("quota low", err.getvalue())

    def test_healthy_quota_never_pauses(self):
        clock = FakeClock()
        fake = FakeGitLab(
            default_routes(),
            headers={"RateLimit-Limit": "2000", "RateLimit-Remaining": "1500", "RateLimit-Reset": str(int(clock.now) + 20)},
        )
        run_pull(fake, clock=clock)
        self.assertEqual(clock.slept, [])

    def test_throttle_floor_scales_with_small_limits(self):
        now = 1000.0
        def response(limit, remaining):
            return Response(200, {"RateLimit-Limit": str(limit), "RateLimit-Remaining": str(remaining), "RateLimit-Reset": "1010"}, b"")
        self.assertEqual(pull.throttle_seconds(response(2000, 50), now=now), 10.0)
        self.assertIsNone(pull.throttle_seconds(response(2000, 51), now=now))
        self.assertIsNone(pull.throttle_seconds(response(60, 7), now=now))
        self.assertEqual(pull.throttle_seconds(response(60, 6), now=now), 10.0)
        self.assertIsNone(pull.throttle_seconds(Response(200, {}, b""), now=now))

    def test_gate_holds_until_extended_window_passes(self):
        clock = FakeClock()
        gate = RateLimitGate(clock=clock.time, sleep=clock.sleep)
        gate.extend(5)
        gate.extend(2)
        gate.wait()
        self.assertEqual(clock.slept, [5.0])
        gate.wait()
        self.assertEqual(clock.slept, [5.0])


class ConfigTest(unittest.TestCase):
    def test_base_url(self):
        self.assertEqual(pull.resolve_base_url(None), "https://gitlab.com")
        self.assertEqual(pull.resolve_base_url("https://gitlab.example.com/"), "https://gitlab.example.com")
        self.assertEqual(pull.resolve_base_url("https://example.com/gitlab"), "https://example.com/gitlab")
        self.assertEqual(pull.resolve_base_url("http://localhost:8929"), "http://localhost:8929")
        for bad in (
            "http://gitlab.example.com",
            "gitlab.com",
            "https://user:pass@gitlab.com",
            "https://gitlab.com/?x=1",
            "https://gitlab.com/api/v4",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(PullError):
                    pull.resolve_base_url(bad)

    def test_project(self):
        with mock.patch.dict(os.environ, {"GITLAB_PROJECT": "acme/widget"}):
            self.assertEqual(pull.resolve_project(None), "acme/widget")
            self.assertEqual(pull.resolve_project("group/sub/proj.name"), "group/sub/proj.name")
            self.assertEqual(pull.resolve_project("42"), "42")
        with mock.patch.dict(os.environ, {"GITLAB_PROJECT": ""}):
            for bad in (None, "widget", "acme/../x y", "acme/widget?x"):
                with self.subTest(bad=bad):
                    with self.assertRaises(PullError):
                        pull.resolve_project(bad)

    def test_concurrency(self):
        with mock.patch.dict(os.environ, {"GITLAB_PULL_CONCURRENCY": ""}):
            self.assertEqual(pull.resolve_concurrency(None), pull.DEFAULT_CONCURRENCY)
        with mock.patch.dict(os.environ, {"GITLAB_PULL_CONCURRENCY": "4"}):
            self.assertEqual(pull.resolve_concurrency(None), 4)
            self.assertEqual(pull.resolve_concurrency(16), 16)
        for bad in (0, pull.MAX_CONCURRENCY + 1):
            with self.assertRaises(PullError):
                pull.resolve_concurrency(bad)
        with mock.patch.dict(os.environ, {"GITLAB_PULL_CONCURRENCY": "many"}):
            with self.assertRaises(PullError):
                pull.resolve_concurrency(None)


class SummaryAndCliTest(unittest.TestCase):
    def test_summary_lists_each_object_once(self):
        capture, _ = run_pull(FakeGitLab(default_routes()))
        summary = pull.render_summary("acme/widget", capture, None)
        self.assertIn("Mode: full backfill. 10 envelopes.", summary)
        self.assertIn("- !1 MR 1 — merged — Why MR 1 exists.", summary)
        self.assertIn("- on !1 — @pat: Decision: keep the intake raw.", summary)
        self.assertIn("- on !1 — @pat (system): approved this merge request", summary)
        self.assertIn("- #10 Issue 10 — opened — Ticket context.", summary)
        self.assertIn("- Skipped confidential issues: 1", summary)
        self.assertIn("- Skipped internal notes: 1", summary)
        reconcile = pull.render_summary("acme/widget", capture, "2026-10-01T00:00:00Z")
        self.assertIn("reconciliation, updated after 2026-10-01T00:00:00Z", reconcile)

    def test_main_writes_jsonl_and_glance_file(self):
        fake = FakeGitLab(default_routes())
        env = {"GITLAB_API_TOKEN": TOKEN, "GITLAB_BASE_URL": BASE, "GITLAB_PROJECT": "acme/widget"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            output = Path(tmp) / "gitlab.jsonl"
            with mock.patch.object(pull, "urllib_exchange", fake), redirect_stderr(io.StringIO()) as err:
                code = pull.main(["--output", str(output)])
            self.assertEqual(code, 0, err.getvalue())
            lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 10)
            for line in lines:
                assert_valid(line)
            self.assertTrue(Path(str(output) + ".readable.md").exists())
            self.assertNotIn(TOKEN, err.getvalue())

    def test_main_stdout_is_pure_jsonl(self):
        fake = FakeGitLab(default_routes())
        env = {"GITLAB_API_TOKEN": TOKEN, "GITLAB_BASE_URL": BASE, "GITLAB_PROJECT": "acme/widget"}
        with mock.patch.dict(os.environ, env), mock.patch.object(pull, "urllib_exchange", fake):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                code = pull.main(["--lookback-hours", "24"])
        self.assertEqual(code, 0)
        for line in out.getvalue().splitlines():
            assert_valid(json.loads(line))

    def test_main_argument_errors(self):
        with mock.patch.dict(os.environ, {"GITLAB_API_TOKEN": ""}), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pull.main([]), 2)
        self.assertIn("GITLAB_API_TOKEN", err.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(pull.main(["--readable", "-"]), 2)
            self.assertEqual(pull.main(["--readable", "x.md", "--no-readable"]), 2)
        env = {"GITLAB_API_TOKEN": TOKEN, "GITLAB_PROJECT": "acme/widget"}
        with mock.patch.dict(os.environ, env), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pull.main(["--updated-after", "2026-10-01T00:00:00Z", "--lookback-hours", "1"]), 1)
        self.assertIn("only one of", err.getvalue())


if __name__ == "__main__":
    unittest.main()
