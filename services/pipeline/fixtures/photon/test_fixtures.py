"""The photon fixtures are valid intake and match the GitHub history.

The fixtures are frozen data: the Slack and Jira pulls that generated them, and the generator, were removed in
EMBER-160 when both connectors moved into ember-backend.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "packages" / "ingestion-envelope"))
sys.path.insert(0, str(HERE))

from validate import assert_valid, load_schema  # noqa: E402

SOURCE = HERE / "photon-source.json"  # the photon GitHub snapshot the fixtures were built from
GITHUB = "https://github.com/ryan-stoffel/photon"
# Ryan is the photon history's only human; Maya and Daniel exist only in Slack and Jira
PEOPLE = {
    "ryan": {"email": "stoffel.thomas.ryan@gmail.com", "slack": "U08RSTOFFEL"},
    "maya": {"email": "maya.okafor@example.com", "slack": "U08MOKAFOR1"},
    "daniel": {"email": "daniel.reyes@example.com", "slack": "U08DREYES01"},
}


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_jsonl(name: str) -> list[dict]:
    with (HERE / name).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def jira_moment(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.000%z")


class PhotonFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = json.loads(SOURCE.read_text(encoding="utf-8"))
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

    def test_every_github_issue_has_one_jira_issue(self):
        issues = [env["body"] for env in self.jira if "fields" in env["body"] and env["body"].get("key", "").startswith("PHO-")]
        mirrored = [i for i in issues if i["fields"]["issuetype"]["name"] != "Epic"]
        self.assertEqual(len(mirrored), len(self.source["issues"]))
        by_summary = {i["fields"]["summary"]: i for i in mirrored}
        for gh in self.source["issues"]:
            with self.subTest(github=gh["number"]):
                issue = by_summary[gh["title"]]
                self.assertEqual(jira_moment(issue["fields"]["created"]), utc(gh["created_at"]))
                if gh["closed_at"]:
                    self.assertEqual(issue["fields"]["status"]["name"], "Done")
                    self.assertEqual(jira_moment(issue["fields"]["resolutiondate"]), utc(gh["closed_at"]))
                else:
                    self.assertNotEqual(issue["fields"]["status"]["name"], "Done")

    def test_remote_links_point_at_real_issues_and_pulls(self):
        known = {f"{GITHUB}/issues/{i['number']}" for i in self.source["issues"]}
        known |= {f"{GITHUB}/pull/{p['number']}" for p in self.source["pulls"]}
        links = [env["body"] for env in self.jira if "globalId" in env["body"]]
        self.assertTrue(links)
        for link in links:
            self.assertIn(link["object"]["url"], known)

    def test_slack_feed_matches_github(self):
        feed = [b for b in (env["body"] for env in self.slack)
                if b.get("type") == "message" and b.get("channel") == "C08PHOTONGH" and b.get("thread_ts", b["ts"]) == b["ts"]]
        self.assertEqual(len(feed), len(self.source["issues"]) + len(self.source["pulls"]))

    def test_people_join_across_sources(self):
        emails = sorted(person["email"] for person in PEOPLE.values())
        self.assertIn(self.source["repo"]["author"]["email"], emails)
        humans = [b for b in (env["body"] for env in self.slack)
                  if "profile" in b and "is_bot" in b and not b["is_bot"] and b["id"] != "USLACKBOT"]
        self.assertEqual(sorted(h["profile"]["email"] for h in humans), emails)
        authors = {b["author"]["emailAddress"] for b in (env["body"] for env in self.jira) if "author" in b}
        self.assertEqual(sorted(authors), emails)
        speakers = {b["user"] for b in (env["body"] for env in self.slack) if b.get("type") == "message" and "bot_id" not in b}
        self.assertEqual(speakers, {person["slack"] for person in PEOPLE.values()})

    def test_pull_requests_stay_ryans(self):
        issues = [env["body"] for env in self.jira if "fields" in env["body"] and env["body"].get("key", "").startswith("PHO-")]
        self.assertEqual({i["fields"]["assignee"]["emailAddress"] for i in issues}, {self.source["repo"]["author"]["email"]})

if __name__ == "__main__":
    unittest.main()
