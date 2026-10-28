# Postgres to Neo4j load check (EMBER-86)

Proves that source data in Postgres reaches Neo4j intact through `python -m loader extract` then `load`
(EMBER-85), and that the graph answers questions the way the source does. One command does it:

    python -m loader e2e-check --tenant <tenant> [--seed-photon] [--reset] [--memory] [--json FILE] [--verbose]

Code: `services/pipeline/loader/e2e_check.py`. Expected values for the photon data:
`services/pipeline/fixtures/photon/expected-graph.json`, written by `fixtures/photon/derive_expected.py`.

## What one run does

1. `--seed-photon` puts the photon Jira and Slack fixtures into `source_records` (what
   `apps/mcp/seed_photon.py --no-embed` does; tenant `photon`).
2. `--reset` deletes the tenant's subgraphs in Neo4j and marks its episodes unloaded, so everything loads again.
3. Runs the extract worker and the load worker until nothing is waiting (the same code as `extract --once`, `load --once`).
4. Reads the graph back. Per subgraph it lists every label and every (source label, relationship type, target
   label) with its count, runs the schema check (`loader/check.py`), and runs the sample queries.

Exit 0 when everything matched. 1 on any count, query or schema mismatch, any episode left unloaded, or any record
extract rejected. 2 when the run could not start (no `DATABASE_URL`, no `NEO4J_PASSWORD`, unreadable expected file).

## Run it on the photon data

Needs a migrated Postgres (as `apps/mcp/README.md`; CI's `Postgres migrations match the ingestion doc` step shows the
container and dbmate) and a Neo4j. From `services/pipeline`:

    export DATABASE_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable'
    export NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_PASSWORD=...
    python -m loader e2e-check --tenant photon --seed-photon --reset

`--memory` loads into an in-memory graph instead of Neo4j: counts and the schema check only, no queries.

## Expected values come from the fixtures, not the loader

`derive_expected.py` is stdlib only and imports nothing from the loader. It counts the intake envelopes
(`jira-intake.jsonl`, `slack-intake.jsonl`) by the graph rules in `docs/graph-schema.md`, and takes each sample
query's answer from the readable transcripts (`jira.md`, `slack.md`). `test_e2e_check.py` fails if the committed
`expected-graph.json` differs from what it derives, and checks its totals against the fixture README
(112 issues, 29 comments, 637 messages, 5 channels). What one record becomes:

- Jira issue: a WorkItem, its project's Container (CONTAINS), the creator and assignee as Identity + Person
  (RESOLVES_TO) with AUTHORED and ASSIGNED_TO, and a RELATES_TO WorkItem edge per issue link (a link shown on both
  issues is one edge).
- Jira comment: a Message, REFERENCES to its issue, its author as Identity + Person with AUTHORED.
- Slack message: a Message, its thread's Conversation (the `thread_ts`, or its own `ts` when it starts one), the
  channel's Container, CONTAINS (channel to thread), PART_OF (message to thread), REPLIES_TO (reply to the thread's
  first message), the sender as Identity + Person with AUTHORED.

## Photon result (real Neo4j 5 community, Postgres 18 with pgvector)

Extract: 778 episodes from 778 source_records (jira issue 112, jira comment 29, slack message 637); 0 rejected,
0 skipped, 0 unloaded, 0 load failures.

| Subgraph | Item | Expected | Actual |
| --- | --- | ---: | ---: |
| photon_jira | node WorkItem | 112 | 112 |
| photon_jira | node Container | 1 | 1 |
| photon_jira | node Message | 29 | 29 |
| photon_jira | node Identity | 3 | 3 |
| photon_jira | node Person | 3 | 3 |
| photon_jira | Container CONTAINS WorkItem | 112 | 112 |
| photon_jira | Person AUTHORED WorkItem | 112 | 112 |
| photon_jira | Person ASSIGNED_TO WorkItem | 112 | 112 |
| photon_jira | WorkItem RELATES_TO WorkItem | 14 | 14 |
| photon_jira | Person AUTHORED Message | 29 | 29 |
| photon_jira | Message REFERENCES WorkItem | 29 | 29 |
| photon_jira | Identity RESOLVES_TO Person | 3 | 3 |
| photon_slack | node Message | 637 | 637 |
| photon_slack | node Conversation | 340 | 340 |
| photon_slack | node Container | 5 | 5 |
| photon_slack | node Identity | 5 | 5 |
| photon_slack | node Person | 5 | 5 |
| photon_slack | Container CONTAINS Conversation | 340 | 340 |
| photon_slack | Message PART_OF Conversation | 637 | 637 |
| photon_slack | Message REPLIES_TO Message | 297 | 297 |
| photon_slack | Person AUTHORED Message | 637 | 637 |
| photon_slack | Identity RESOLVES_TO Person | 5 | 5 |

Both subgraphs pass the schema check with no mismatches. Every row matches; there are no labels or relationship types
in the graph that the expected file lacks (a row is added for those too, with expected 0).

## The five sample queries

Parameters: `$jira` = `photon_jira`, `$slack` = `photon_slack`. The Cypher is in `expected-graph.json`
(`queries[].cypher`); the answers are read from the transcripts. All five returned the expected rows.

| # | Question | Source of the answer | Result |
| --- | --- | --- | --- |
| 1 | Who commented on PHO-48? | `jira.md`, PHO-48's comment lines | Daniel Reyes, Ryan Stoffel |
| 2 | Which messages are in the #photon-bugs thread that starts "0.3.0 is bad. Photon shows up ...", and who sent them? | `slack.md`, #photon-bugs parent line and indented replies | In order: U08DREYES01 "0.3.0 is bad. ..." then U08RSTOFFEL "Filed as GH-107." |
| 3 | Which issues are linked to PHO-19 ("relates to")? | `jira.md`, "relates to" lines of PHO-19 and of issues naming it | PHO-10, PHO-12 |
| 4 | What are PHO-19's type and status, and how many issues are in each status? | `jira.md`, status line of every issue | PHO-19 is a Task in To Do; Done 109, In Progress 2, To Do 1 |
| 5 | Does anything cross from the Jira subgraph into the Slack one? | the data (each source loaded alone) | photon_jira: 148 nodes, source `jira` only, 0 crossing edges; photon_slack: 992 nodes, source `slack` only, 0 crossing |

The photon data has no deleted or edited Jira issue or Slack message (no `message_changed`, `message_deleted`, no
edited subtype), so no transcript says what a deleted or edited item looks like. Query 4 covers an item's state;
the deleted and edited cases are covered by `loader/test_pg_load.py` and `test_extract.py` (EMBER-85) and, on real
data, by the `deleted-state` smoke query below.

## GitHub (EMBER-107)

`python -m loader extract` maps the `source_records` that `enrich:github` writes
(`services/github-ingestion/enrich.py`, `to_records`) into the `<tenant>_github` subgraph, with the same ordering,
delete and placeholder rules as Jira and Slack. Code: `loader/mappers/github.py` (`map_github_record`,
`github_tombstone`). Only types `docs/graph-schema.md` already defines are used. Every node's key is the row's
`object_id` where the object has one.

| `source_records` kind | Becomes |
| --- | --- |
| `repository` | Container (`<owner>/<repo>`, name and URL) |
| `issue` | WorkItem `<repo>#<n>` (title + body, `status`, `item_type: issue`), Container CONTAINS it, author AUTHORED, assignees ASSIGNED_TO |
| `pull_request` | Change `<repo>#<n>` (`kind: pull_request`, `state` = `merged` once `merged_at` is set, else `open`/`closed`, `source_branch`, `target_branch`), CONTAINS, AUTHORED, ASSIGNED_TO, RESOLVES WorkItem for each "fixes/closes/resolves #n" in the body. `record.fetched.files` (opened/reopened/synchronize): a Module per directory holding a changed file (`path`, `.` for the root), Container CONTAINS Module, Change TOUCHES Module |
| `pull_request_review` | REVIEWED Person to the pull request (`verdict` = the review's state, lowercased). A review with text is also a Message (`kind: review`) with AUTHORED and REFERENCES to the pull request |
| `issue_comment` | Message (`kind: comment`) with AUTHORED and REFERENCES to the issue (WorkItem) or, when `issue.pull_request` is set, the pull request (Change) |
| `pull_request_review_comment` | Message (`kind: comment`), REFERENCES the pull request, REPLIES_TO the comment in `in_reply_to_id` |
| `commit_comment` | Message (`kind: comment`), REFERENCES the commit's Change |
| `commit` | Change `<repo>@<id>` (`kind: commit`, first message line as name, `state: committed`), keyed by the push payload's `id` (or the compare API's `sha`); CONTAINS; AUTHORED by `author.username` when GitHub sends one; TOUCHES a Module per directory of `added`/`modified`/`removed` |
| `ref`, `release`, `label`, `milestone` | Not extracted (counted as `not extracted github/<kind>`): the schema has no branch, tag, release, label or milestone type, so nothing of them, and nothing of a deleted ref, reaches the graph |

- **Parents carried in payloads.** A comment or review payload carries its issue or pull request. It is written
  too, stamped with its own `updated_at` (the ordering value the object's own rows use), so it fills a parent
  whose own events never arrived (older than the App's install) and never overrides a newer version.
- **Deletes.** A row with `tombstone: true` (`issues`/`*_comment`/`repository` `deleted`) becomes a tombstone node:
  the WorkItem, Message or Container keeps only its key and `deleted: true`.
- **Ordering.** Every version applies by `occurred_at` (enrich's ordering value: `updated_at`, `submitted_at`,
  `pushed_at`, or `received_at` for deletes, labels and repository events).
- Commit author and committer emails and names are not stored (GitHub's commit emails are unverified).

### Synthetic check

`loader/testdata_github.py` holds 31 made-up rows shaped like real `enrich:github` output (key paths and types taken
from the team's records, no values): every kind above, edits, two deletes, a review without text, a comment on a
pull request older than the data, late older versions and the four unmapped kinds. `test_extract_github.py` and
`test_pg_load.py` check the graph they load to against counts made by hand. Through the CLI
(`python -m loader e2e-check --tenant synthetic-gh`, local Postgres 18 + `neo4j:5-community`): 25 episodes, 0
rejected, 0 unloaded; not extracted ref 3, release 2, label 1. Nodes: Change 6, Message 9, WorkItem 3 (1
placeholder), Module 4, Identity 4, Person 4, Container 1. Schema check OK, all six smoke queries pass, and the
output holds counts only. The real-data run (Parallax) is for the Lead.

## Real data (any populated tenant)

For a Postgres that already holds `source_records` (no seeding), give the tenant:

    python -m loader e2e-check --tenant ember-development

Run it from `services/pipeline` with `DATABASE_URL`, `NEO4J_URI` and `NEO4J_PASSWORD` set to the local Postgres and
Neo4j. Add nothing else: with no expected file (only the tenant `photon` has one by default; `--expected FILE` gives
another) it runs extract and load on whatever is waiting, then reports:

- per label and per relationship type counts for each of the tenant's subgraphs (`<tenant>_jira`, `<tenant>_slack`,
  `<tenant>_github`), reported, not compared;
- the schema-check result (counts of mismatches by kind, such as unknown labels or missing constraints);
- `source_records` counts by source and kind, episodes, unloaded, and `rejected`, `skipped` and `not extracted`
  counts by `source/kind`;
- six smoke queries, the same five shapes as above with no photon-specific answer (comment authors, thread
  messages, issue references, deleted-item state and subgraph isolation, the last two across Jira, Slack and
  GitHub) plus `github-changes` (pull requests, commits, placeholders, and how many have an author, a review, a
  comment, a touched Module). Each prints only a row of counts and passes or fails on a rule (for example
  `crossing = 0`, `still_holding_content = 0`, `pull_requests + commits + placeholders = changes`);
- `placeholder <Label>` rows: bare nodes an edge created whose own record never loaded (for GitHub, mostly
  "closes #n" naming an issue that is not in the data). They are reported, not failed.

The output holds no message text, ticket text, names, keys or ids: counts, kinds and pass/fail only. The workers'
own log lines (record ids, rejection reasons) are silenced unless `--verbose`. `--json FILE` writes the same
content. It never resets unless `--reset` is given. Extract and load are not limited to the tenant: they take every
tenant's waiting rows in that Postgres.

Exit 0 pass, 1 mismatch or leftovers, 2 could not start.

### Real data (ember-development): results

Run on 2026-10-28 against a one-time, read-only copy of the team's own Slack records (tenant `ember-development`),
taken from the cluster Postgres with the owner's approval. They were loaded into a local Postgres 18 (pgvector 0.8.7,
migrated by dbmate v2.36.0) and a local `neo4j:5-community`. The copy was deleted after the run, and only the counts
below were kept: no message text, names or ids.

- `source_records`: slack/message 87, slack/channel 3, slack/user 3. No Jira records were on the cluster yet; the
  Jira part runs when they arrive.
- Episodes: 93 extracted, 93 loaded, 0 unloaded, 0 load failures. Rejected 0, skipped 0, not extracted 0.
- Nodes in `ember-development_slack`: Message 61, Conversation 48, Container 3, Identity 3, Person 3. The 87 message
  records are 61 distinct messages; the rest are later versions of the same message. So 61 Message nodes is the
  expected result.
- Relationships: PART_OF 61, AUTHORED 61, CONTAINS 48, REPLIES_TO 13, RESOLVES_TO 3.
- Schema check: OK.
- Smoke queries: all 5 pass.
  - thread-messages: 48 threads, the largest with 3 messages; all 61 messages have a sender.
  - subgraph-isolation: 118 nodes, 0 with the wrong source, 0 crossing edges.
  - deleted-state: 0 deleted, 0 still holding content (the copy had no deletes).
  - comment-authors and issue-references: 0, since Jira isn't present yet.
- Result: **PASS**. Follow-ups: rerun with Jira once its records reach the cluster. The GitHub (Parallax) run is
  EMBER-107.

## Gaps in the ontology and mapping, with suggested tickets

Found by comparing what the photon fixtures carry with what lands in the graph.

1. **No epic-to-child link.** 104 of the 112 photon issues have a `parent` (their epic), and the graph has no edge
   for it: the epic is a WorkItem with nothing under it. The ontology has no WorkItem-to-WorkItem hierarchy edge
   (`RELATES_TO` is the only one). Suggested ticket: add a hierarchy edge (for example `PART_OF` WorkItem to
   WorkItem, or a new `CHILD_OF`) to `ontology/edges.py`, `schema.cypher` and the Jira mapper, plus the epic's
   `item_type` handling. Needs an ontology decision first.
2. **Jira remote links are dropped.** 179 remote links point from issues to GitHub issues and pull requests (`GH-n`),
   and Slack text says "Filed as GH-107". Neither becomes an edge, so a Jira issue, its GitHub issue and the Slack
   thread are never connected. Suggested ticket: cross-source reference edges (REFERENCES to a GitHub WorkItem by
   key), which needs the GitHub subgraph and the cross-subgraph rule in `docs/graph-schema.md` settled.
3. **Same person across sources is three Person nodes.** The photon people share an email across Slack and Jira,
   but each subgraph gets its own Person (Slack 5, Jira 3), by design of one subgraph per source. Suggested ticket:
   decide where identity resolution to one Person lives (a cross-subgraph SAME_AS, or a later merge stage).
4. **Slack users and channels never reach the graph in the photon load.** `seed_photon.py` stores only messages, so
   Slack Persons have an Identity id but no display name or email, and Containers are named by channel id. The
   extractor supports user and channel records. Suggested ticket: make the seed (and the real Slack enrich) store
   `user` and `channel` source_records, and add them to this check.
5. **Not extracted: Jira worklogs, attachments, remote links, changelog, sprints, versions, watchers, votes; Slack
   reactions, files, memberships.** Counts are reported as `not extracted` / `skipped` by kind. Suggested ticket per
   value: decide which are graph facts (sprints and versions look like Containers; attachments like Documents).
6. **Photon has no deleted or edited items.** The delete and late-edit rules of EMBER-85 cannot be checked against
   a transcript. Suggested ticket: add a deleted issue, a deleted message and an edited comment to the photon
   generator, with the transcript lines, then extend `derive_expected.py`.
7. **Extract and load are not tenant-scoped.** `python -m loader extract` and `load` take every tenant's waiting
   rows, so a check on one tenant also loads the rest. Suggested ticket: `--tenant` on both workers.
8. **GitHub refs, releases, labels and milestones are not in the graph** (EMBER-107). The schema has no branch,
   tag, release, label or milestone type, and Change only has `source_branch`/`target_branch`. Suggested ticket:
   decide whether releases are a Change or Decision kind, labels a WorkItem/Change attribute (list of names), and
   whether branches need a node; refs are mostly noise (156 of 197 real ref objects are deleted branches).
9. **"closes #n" guesses an issue.** GitHub issues and pull requests share numbers, so a pull request naming another
   pull request gets a WorkItem placeholder that never fills. Suggested ticket: resolve `#n` at load time against the
   subgraph (Change or WorkItem), or emit REFERENCES when the target is a Change.
10. **The loader never removes edges.** A file dropped from a pull request by a later push keeps its TOUCHES edge, an
    unassigned person keeps ASSIGNED_TO, a removed "closes #n" keeps RESOLVES, and a re-review keeps one REVIEWED
    edge with the latest verdict. Suggested ticket: edge validity (`invalid_at`) per ADR 0002's time model.
11. **Commits are not linked to their pull request or branch.** No edge type joins two Changes except REFERENCES, and
    push payloads do not name the pull request. Suggested ticket: a `PART_OF`-like Change to Change edge, filled
    from the PR commits API or the merge commit.
12. **A renamed or transferred repository becomes a second Container** (keyed by `full_name`). Suggested ticket: key
    GitHub Containers by `repository.id` with `full_name` as the name, which needs a rebuild of loaded subgraphs.
13. **Module granularity.** A Module is the directory of each changed file, so deep trees give many small Modules
    and root files share one `.` Module. Suggested ticket: decide Module granularity with the code layer (phase 3).
