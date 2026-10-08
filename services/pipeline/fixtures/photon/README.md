# Photon synthetic Slack and Jira data

Jira: EMBER-43. Parent epic: EMBER-4. Floor.

A fake Slack workspace and a fake Jira board for [ryan-stoffel/photon](https://github.com/ryan-stoffel/photon), built from that repo's real GitHub issues, pull requests, comments, and releases (2026-09-14 to 2026-10-04). Use it to exercise the pipeline (EMBER-3) with Slack and Jira intake that lines up with a GitHub history.

## Files

| File | What it is |
| --- | --- |
| `slack-intake.jsonl` | EMBER-39 `{type, body}` envelopes, one per line, in the order the Slack pull emits them |
| `jira-intake.jsonl` | Same for the Jira pull |
| `slack.md`, `jira.md` | Full-text transcripts for people: every message and thread, every issue with its description, comments, links, and history |
| `*.jsonl.readable.md` | The connectors' own glance summaries, as `pull_test.py --output` writes them |
| `photon-source.json` | Snapshot of the photon GitHub data the generator reads |
| `generate.py` | Builds everything above. Stdlib only |
| `test_fixtures.py` | Schema, freshness, and consistency checks |

## How the JSONL is made

`generate.py` does not write envelopes itself. It answers the Slack Web API and the Jira REST API from memory and runs the real pull code in `services/slack-ingestion/pull_test.py` and `services/jira-ingestion/pull_test.py` against those fakes. The JSONL is what the connectors would emit for this workspace and board: same object shapes, same order, same `channel` field added to history messages, same changelog page beans.

```sh
python3 services/pipeline/fixtures/photon/generate.py
python3 services/pipeline/fixtures/photon/test_fixtures.py
```

The test fails if a committed file differs from a fresh run, so a connector change that alters its output shows up here.

## What is real and what is made up

Taken from GitHub unchanged:

- Issue titles, bodies, labels, open and close times, and close reasons
- Ryan's issue comments
- Pull request numbers, titles, branches, and open, merge, and close times
- Release tags, publish times, and the one-line summary from `CHANGELOG.md`

Derived from those:

- One Jira issue per GitHub issue (104), with the same summary and description (converted to ADF), created and resolved at the GitHub times. Remote links point at the GitHub issue and at every pull request whose branch names it (`GH-<n>`).
- Issue type from `type/*` labels or the title prefix, priority from `priority/p0`..`p3`, epic from `area/*`. Status moves to In Progress when the first linked pull request opens and to Done when the GitHub issue closes. The fix version is the first release published after that.
- 8 epics, 4 weekly sprints, 25 released versions, 14 issue links.
- Slack `#photon-github`: the GitHub app posts each issue and pull request, with the close or merge as a thread reply. `#photon-releases`: each release. `#photon-bugs`: Jira Cloud posts each new Bug.

Written for this fixture:

- Two teammates, Maya Okafor (design) and Daniel Reyes (QA and release testing). See People.
- The human messages in `#general`, `#photon-dev`, `#photon-bugs`, and `#photon-releases`, and the Jira comments that are not copied from GitHub. Each one restates a decision, bug report, or verification the GitHub history already shows (the GPUI rewrite and the rollback to Swift, the parity gate, branch names without dots, the Homebrew tap rename, the onboarding rewrite), placed at the time the history puts that work.
- All ids, the `photon-synthetic` Slack and Atlassian domains, and the Jira account ids.

## People

The fixture is a three-person team:

| Person | Role | Email (join key) |
| --- | --- | --- |
| Ryan Stoffel | Engineer. Writes every pull request | `stoffel.thomas.ryan@gmail.com`, from the commit history |
| Maya Okafor | Design. Reports Stories for the launcher, clipboard, notes, Settings, and onboarding | `maya.okafor@example.com` |
| Daniel Reyes | QA and release testing. Reports every Bug and verifies releases | `daniel.reyes@example.com` |

Maya and Daniel are fictional. They exist only in Slack and Jira, because every commit, issue, and pull request in photon is Ryan's. Ryan stays the Jira assignee on every issue so Jira agrees with GitHub about who did the work. Maya and Daniel show up as reporters, commenters, watchers, and in Slack threads. The same email is on each person's Slack profile and Jira user, so the pipeline can join people across sources.

Slack members: the three people, Slackbot, and the GitHub, Jira Cloud, and Ember bots. `#random` exists but Ember is not a member, so the pull skips it, the same as a real workspace.

## Counts

- Slack: 650 envelopes. 1 workspace, 7 members, 5 channels, 637 messages and replies.
- Jira: 716 envelopes. 1 project, 25 versions, 4 status sets, 112 issues (8 epics), 28 issue link views, 29 comments, 112 changelog pages, 2 attachments, 179 remote links, 112 watcher and 112 vote objects.
