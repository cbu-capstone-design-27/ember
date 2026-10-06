"""The photon fixtures are valid intake, current with the generator, and match the GitHub history."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(HERE))

import generate  # noqa: E402
from validate import assert_valid, load_schema  # noqa: E402

OUTPUTS = (
    "slack-intake.jsonl",
    "slack-intake.jsonl.readable.md",
    "slack.md",
    "jira-intake.jsonl",
    "jira-intake.jsonl.readable.md",
    "jira.md",
)


def read_jsonl(name: str) -> list[dict]:
    with (HERE / name).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def jira_moment(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.000%z")


class PhotonFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = json.loads(generate.SOURCE.read_text(encoding="utf-8"))
        cls.slack = read_jsonl("slack-intake.jsonl")
        cls.jira = read_jsonl("jira-intake.jsonl")

    def test_every_line_is_intake(self):
        schema = load_schema()
        for name, envelopes, source in (("slack", self.slack, "slack"), ("jira", self.jira, "jira")):
            with self.subTest(file=name):
                self.assertTrue(envelopes)
                for envelope in envelopes:
                    assert_valid(envelope, schema)
                    self.assertEqual(envelope["type"], source)

    def test_committed_files_match_the_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            generate.generate(Path(tmp))
            for name in OUTPUTS:
                with self.subTest(file=name):
                    fresh = (Path(tmp) / name).read_bytes()
                    self.assertEqual(fresh, (HERE / name).read_bytes(), f"{name} is stale; run generate.py")

    def test_every_github_issue_has_one_jira_issue(self):
        issues = [env["body"] for env in self.jira if "fields" in env["body"] and env["body"].get("key", "").startswith("PHO-")]
        mirrored = [i for i in issues if i["fields"]["issuetype"]["name"] != "Epic"]
        self.assertEqual(len(mirrored), len(self.source["issues"]))
        by_summary = {i["fields"]["summary"]: i for i in mirrored}
        for gh in self.source["issues"]:
            with self.subTest(github=gh["number"]):
                issue = by_summary[gh["title"]]
                self.assertEqual(jira_moment(issue["fields"]["created"]), generate.utc(gh["created_at"]))
                if gh["closed_at"]:
                    self.assertEqual(issue["fields"]["status"]["name"], "Done")
                    self.assertEqual(jira_moment(issue["fields"]["resolutiondate"]), generate.utc(gh["closed_at"]))
                else:
                    self.assertNotEqual(issue["fields"]["status"]["name"], "Done")

    def test_remote_links_point_at_real_issues_and_pulls(self):
        known = {f"{generate.GITHUB}/issues/{i['number']}" for i in self.source["issues"]}
        known |= {f"{generate.GITHUB}/pull/{p['number']}" for p in self.source["pulls"]}
        links = [env["body"] for env in self.jira if "globalId" in env["body"]]
        self.assertTrue(links)
        for link in links:
            self.assertIn(link["object"]["url"], known)

    def test_slack_feed_matches_github(self):
        feed = [b for b in (env["body"] for env in self.slack)
                if b.get("type") == "message" and b.get("channel") == "C08PHOTONGH" and b.get("thread_ts", b["ts"]) == b["ts"]]
        self.assertEqual(len(feed), len(self.source["issues"]) + len(self.source["pulls"]))

    def test_people_are_the_people_in_the_history(self):
        humans = [b for b in (env["body"] for env in self.slack)
                  if "profile" in b and "is_bot" in b and not b["is_bot"] and b["id"] != "USLACKBOT"]
        self.assertEqual([h["profile"]["email"] for h in humans], [self.source["repo"]["author"]["email"]])
        authors = {b["author"]["emailAddress"] for b in (env["body"] for env in self.jira) if "author" in b}
        self.assertEqual(authors, {self.source["repo"]["author"]["email"]})


if __name__ == "__main__":
    unittest.main()
