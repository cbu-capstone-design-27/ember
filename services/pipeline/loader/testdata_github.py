"""Synthetic enrich:github source_records rows for the GitHub extract and load tests (EMBER-107).

Every row has the shape services/github-ingestion/enrich.py's to_records writes, and every payload the
key structure of real GitHub deliveries (the key paths and types were taken from the team's real records;
no value was). Everything here is made up: the organisation, the people, the text, the ids and the SHAs.

rows() returns (source, kind, object_id, version, occurred_at, record) tuples in the order a test inserts
them; occurred_at is the ordering value enrich sets (docs/ingestion/github-webhooks.md, Ordering value).
EXPECTED holds the graph they load to, counted by hand from the rows below by the rules in
docs/pipeline/neo4j-load-check.md (GitHub), not by running the mapper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
OWNER = "lumen-labs"
REPO = f"{OWNER}/prism"
REPO_ID = 900001
API = f"https://api.github.com/repos/{REPO}"
WEB = f"https://github.com/{REPO}"
PEOPLE = {"ada-fake": 1001, "bo-fake": 1002, "cy-fake": 1003, "dee-fake": 1004}
SHA = {name: (name * 40)[:40] for name in ("c1a", "c2b", "c3c", "e4d", "f5e")}  # hex-looking, not real


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iso(minutes: float) -> str:
    return at(minutes).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(login: str) -> dict:
    uid = PEOPLE.get(login, 2000)
    base = f"https://api.github.com/users/{login}"
    return {
        "login": login, "id": uid, "node_id": f"U_fake{uid}", "avatar_url": f"https://avatars.example.invalid/u/{uid}",
        "gravatar_id": "", "url": base, "html_url": f"https://github.com/{login}", "followers_url": f"{base}/followers",
        "following_url": f"{base}/following{{/other_user}}", "gists_url": f"{base}/gists{{/gist_id}}",
        "starred_url": f"{base}/starred{{/owner}}{{/repo}}", "subscriptions_url": f"{base}/subscriptions",
        "organizations_url": f"{base}/orgs", "repos_url": f"{base}/repos", "events_url": f"{base}/events{{/privacy}}",
        "received_events_url": f"{base}/received_events", "type": "User", "user_view_type": "public",
        "site_admin": False,
    }


def repository(push: bool = False) -> dict:
    owner = user(OWNER) | {"type": "Organization"}
    if push:
        owner |= {"name": OWNER, "email": None}
    repo = {
        "id": REPO_ID, "node_id": "R_fake900001", "name": "prism", "full_name": REPO, "private": True, "owner": owner,
        "html_url": WEB, "description": "A made-up repository for tests", "fork": False, "url": API,
        "forks_url": f"{API}/forks", "keys_url": f"{API}/keys{{/key_id}}", "collaborators_url": f"{API}/collaborators",
        "teams_url": f"{API}/teams", "hooks_url": f"{API}/hooks", "issue_events_url": f"{API}/issues/events",
        "events_url": f"{API}/events", "assignees_url": f"{API}/assignees", "branches_url": f"{API}/branches",
        "tags_url": f"{API}/tags", "blobs_url": f"{API}/git/blobs", "git_tags_url": f"{API}/git/tags",
        "git_refs_url": f"{API}/git/refs", "trees_url": f"{API}/git/trees", "statuses_url": f"{API}/statuses/{{sha}}",
        "languages_url": f"{API}/languages", "stargazers_url": f"{API}/stargazers", "contributors_url": f"{API}/contributors",
        "subscribers_url": f"{API}/subscribers", "subscription_url": f"{API}/subscription", "commits_url": f"{API}/commits",
        "git_commits_url": f"{API}/git/commits", "comments_url": f"{API}/comments", "issue_comment_url": f"{API}/issues/comments",
        "contents_url": f"{API}/contents/{{+path}}", "compare_url": f"{API}/compare/{{base}}...{{head}}",
        "merges_url": f"{API}/merges", "archive_url": f"{API}/{{archive_format}}{{/ref}}", "downloads_url": f"{API}/downloads",
        "issues_url": f"{API}/issues{{/number}}", "pulls_url": f"{API}/pulls{{/number}}", "milestones_url": f"{API}/milestones",
        "notifications_url": f"{API}/notifications", "labels_url": f"{API}/labels{{/name}}", "releases_url": f"{API}/releases",
        "deployments_url": f"{API}/deployments", "created_at": "2026-09-01T09:00:00Z", "updated_at": iso(-60),
        "pushed_at": iso(4), "git_url": f"git://github.com/{REPO}.git", "ssh_url": f"git@github.com:{REPO}.git",
        "clone_url": f"{WEB}.git", "svn_url": WEB, "homepage": "", "size": 120, "stargazers_count": 0,
        "watchers_count": 0, "language": "Python", "has_issues": True, "has_projects": True, "has_downloads": True,
        "has_wiki": False, "has_pages": False, "has_discussions": False, "has_pull_requests": True,
        "pull_request_creation_policy": "all", "forks_count": 0, "mirror_url": None, "archived": False,
        "disabled": False, "open_issues_count": 2,
        "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT", "url": "https://api.github.com/licenses/mit",
                    "node_id": "L_fake"},
        "allow_forking": True, "is_template": False, "web_commit_signoff_required": False, "topics": [],
        "visibility": "private", "forks": 0, "open_issues": 2, "watchers": 0, "default_branch": "main",
    }
    if push:  # push payloads send these as epoch seconds and add stargazers and master_branch
        repo |= {"created_at": int(datetime(2026, 9, 1, 9, tzinfo=timezone.utc).timestamp()),
                 "pushed_at": int(at(4).timestamp()), "stargazers": 0, "master_branch": "main"}
    return repo


def envelope(action: str | None, sender: str, push: bool = False, **body) -> dict:
    out = {} if action is None else {"action": action}
    out.update(body)
    out.update(repository=repository(push), sender=user(sender),
               installation={"id": 7000001, "node_id": "MDIzOkludGVncmF0aW9uSW5zdGFsbGF0aW9uZmFrZQ=="})
    return out


def reactions(url: str) -> dict:
    return {"url": f"{url}/reactions", "total_count": 0, "+1": 0, "-1": 0, "laugh": 0, "hooray": 0, "confused": 0,
            "heart": 0, "rocket": 0, "eyes": 0}


def issue(number: int, title: str, body: str, author: str, updated: float, *, state: str = "open",
          assignees: tuple = (), pull: dict | None = None, created: float = 0) -> dict:
    url = f"{API}/issues/{number}"
    obj = {
        "url": url, "repository_url": API, "labels_url": f"{url}/labels{{/name}}", "comments_url": f"{url}/comments",
        "events_url": f"{url}/events", "html_url": f"{WEB}/{'pull' if pull else 'issues'}/{number}",
        "id": 3000000 + number, "node_id": f"I_fake{number}", "number": number, "title": title, "user": user(author),
        "labels": [], "state": state, "locked": False, "assignee": user(assignees[0]) if assignees else None,
        "assignees": [user(a) for a in assignees], "milestone": None, "comments": 1, "created_at": iso(created),
        "updated_at": iso(updated), "closed_at": iso(updated) if state == "closed" else None,
        "author_association": "MEMBER", "active_lock_reason": None, "body": body, "reactions": reactions(url),
        "timeline_url": f"{url}/timeline", "performed_via_github_app": None,
        "state_reason": "completed" if state == "closed" else None,
    }
    if pull is None:
        obj.update(sub_issues_summary={"total": 0, "completed": 0, "percent_completed": 0},
                   issue_dependencies_summary={"blocked_by": 0, "total_blocked_by": 0, "blocking": 0, "total_blocking": 0},
                   pinned_comment=None)
    else:
        obj.update(draft=False, pull_request={"url": f"{API}/pulls/{number}", "html_url": f"{WEB}/pull/{number}",
                                              "diff_url": f"{WEB}/pull/{number}.diff",
                                              "patch_url": f"{WEB}/pull/{number}.patch", **pull})
    return obj


def pull_request(number: int, title: str, body: str, author: str, updated: float, *, head: str, state: str = "open",
                 merged_at: float | None = None, assignees: tuple = (), in_review: bool = False,
                 created: float = 0) -> dict:
    url = f"{API}/pulls/{number}"
    side = lambda ref, sha: {"label": f"{OWNER}:{ref}", "ref": ref, "sha": sha, "user": user(OWNER),  # noqa: E731
                             "repo": repository()}
    obj = {
        "url": url, "id": 4000000 + number, "node_id": f"PR_fake{number}", "html_url": f"{WEB}/pull/{number}",
        "diff_url": f"{WEB}/pull/{number}.diff", "patch_url": f"{WEB}/pull/{number}.patch",
        "issue_url": f"{API}/issues/{number}", "number": number, "state": state, "locked": False, "title": title,
        "user": user(author), "body": body, "created_at": iso(created), "updated_at": iso(updated),
        "closed_at": iso(updated) if state == "closed" else None,
        "merged_at": iso(merged_at) if merged_at is not None else None,
        "merge_commit_sha": SHA["f5e"], "assignee": user(assignees[0]) if assignees else None,
        "assignees": [user(a) for a in assignees], "requested_reviewers": [], "requested_teams": [], "labels": [],
        "milestone": None, "draft": False, "commits_url": f"{url}/commits", "review_comments_url": f"{url}/comments",
        "review_comment_url": f"{API}/pulls/comments{{/number}}", "comments_url": f"{API}/issues/{number}/comments",
        "statuses_url": f"{API}/statuses/{SHA['c3c']}", "head": side(head, SHA["c3c"]), "base": side("main", SHA["e4d"]),
        "_links": {"self": {"href": url}, "html": {"href": f"{WEB}/pull/{number}"}},
        "author_association": "MEMBER", "auto_merge": None, "active_lock_reason": None,
    }
    if not in_review:  # pull_request events add the merge and size fields; review payloads lack them
        obj.update(merged=merged_at is not None, mergeable=None, rebaseable=None, mergeable_state="unknown",
                   merged_by=user("bo-fake") if merged_at is not None else None, comments=1, review_comments=1,
                   maintainer_can_modify=False, commits=3, additions=40, deletions=8, changed_files=4)
    return obj


def files(*entries: tuple[str, str]) -> dict:
    return {"files": [{"sha": SHA["e4d"], "filename": name, "status": status, "additions": 5, "deletions": 1,
                       "changes": 6, "blob_url": f"{WEB}/blob/{SHA['c3c']}/{name}",
                       "raw_url": f"{WEB}/raw/{SHA['c3c']}/{name}", "contents_url": f"{API}/contents/{name}",
                       "patch": "@@ -1,1 +1,1 @@\n-old line\n+new line"} for status, name in entries]}


def comment(cid: int, body: str, author: str, created: float, updated: float, issue_number: int) -> dict:
    url = f"{API}/issues/comments/{cid}"
    return {"url": url, "html_url": f"{WEB}/issues/{issue_number}#issuecomment-{cid}",
            "issue_url": f"{API}/issues/{issue_number}", "id": cid, "node_id": f"IC_fake{cid}", "user": user(author),
            "created_at": iso(created), "updated_at": iso(updated), "author_association": "MEMBER", "body": body,
            "reactions": reactions(url), "performed_via_github_app": None, "minimized": None}


def review(rid: int, state: str, body: str, author: str, submitted: float, number: int) -> dict:
    return {"id": rid, "node_id": f"PRR_fake{rid}", "user": user(author), "body": body, "commit_id": SHA["c3c"],
            "submitted_at": iso(submitted), "updated_at": iso(submitted), "state": state,
            "html_url": f"{WEB}/pull/{number}#pullrequestreview-{rid}", "pull_request_url": f"{API}/pulls/{number}",
            "author_association": "MEMBER",
            "_links": {"html": {"href": f"{WEB}/pull/{number}#pullrequestreview-{rid}"},
                       "pull_request": {"href": f"{API}/pulls/{number}"}}}


def review_comment(cid: int, body: str, author: str, minutes: float, number: int, reply_to: int | None = None) -> dict:
    url = f"{API}/pulls/comments/{cid}"
    obj = {"url": url, "pull_request_review_id": 6100 + cid, "id": cid, "node_id": f"PRRC_fake{cid}",
           "diff_hunk": "@@ -1,1 +1,1 @@", "path": "src/app/main.py", "commit_id": SHA["c3c"],
           "original_commit_id": SHA["c3c"], "user": user(author), "body": body, "created_at": iso(minutes),
           "updated_at": iso(minutes), "html_url": f"{WEB}/pull/{number}#discussion_r{cid}",
           "pull_request_url": f"{API}/pulls/{number}", "author_association": "MEMBER",
           "_links": {"self": {"href": url}}, "reactions": reactions(url), "start_line": None, "original_start_line": None,
           "start_side": None, "line": 3, "original_line": 3, "side": "RIGHT", "original_position": 1, "position": 1,
           "subject_type": "line"}
    if reply_to is not None:
        obj["in_reply_to_id"] = reply_to
    return obj


def push_commit(sha: str, message: str, author: str, minutes: float, added=(), modified=(), removed=()) -> dict:
    person = {"name": f"{author} name", "email": f"{author}@example.invalid", "username": author}
    return {"id": sha, "tree_id": SHA["e4d"], "distinct": True, "message": message, "timestamp": iso(minutes),
            "url": f"{WEB}/commit/{sha}", "author": dict(person), "committer": dict(person),
            "added": list(added), "removed": list(removed), "modified": list(modified)}


def rows(tenant_unused: str | None = None) -> list[tuple]:
    """The rows, in insertion order (not the source's order: a few arrive late on purpose)."""
    out: list[tuple] = []

    def add(kind, object_id, version, minutes, payload, *, fetched=None, tombstone=False):
        record: dict = {"payload": payload}
        if fetched:
            record["fetched"] = fetched
        if tombstone:
            record["tombstone"] = True
        out.append(("github", kind, object_id, version, at(minutes), record))

    received = lambda m: at(m).isoformat()  # noqa: E731 enrich's <received_at> versions

    # repository: edited (its description); becomes the Container
    add("repository", str(REPO_ID), received(0), 0,
        envelope("edited", "ada-fake", changes={"description": {"from": "An older made-up description"}}))

    # issue #1: opened, then closed; issue #2: opened, then deleted
    add("issue", f"{REPO}#1", iso(1), 1, envelope("opened", "ada-fake", issue=issue(
        1, "Startup shows a blank window", "Steps: open the app twice.", "ada-fake", 1, assignees=("bo-fake",))))
    add("issue", f"{REPO}#1", iso(50), 50, envelope("closed", "bo-fake", issue=issue(
        1, "Startup shows a blank window", "Steps: open the app twice.", "ada-fake", 50, state="closed",
        assignees=("bo-fake",))))
    add("issue", f"{REPO}#2", iso(2), 2, envelope("opened", "cy-fake", issue=issue(
        2, "Private notes pasted by mistake", "SECRET-ISSUE-TEXT", "cy-fake", 2)))
    add("issue", f"{REPO}#2", f"deleted:{received(60)}", 60, envelope("deleted", "cy-fake", issue=issue(
        2, "Private notes pasted by mistake", "SECRET-ISSUE-TEXT", "cy-fake", 2)), tombstone=True)

    # pull request #3: opened (files), synchronize (files), edited (title), closed and merged
    pr3 = dict(number=3, body="Fixes #1. Also closes #7.", author="ada-fake", head="fast-start")
    add("pull_request", f"{REPO}#3", iso(3), 3, envelope("opened", "ada-fake", number=3, pull_request=pull_request(
        title="Speed up startup", updated=3, **pr3)),
        fetched=files(("modified", "src/app/main.py"), ("added", "src/app/util.py"), ("modified", "README.md")))
    add("pull_request", f"{REPO}#3", iso(10), 10, envelope(
        "synchronize", "ada-fake", number=3, before=SHA["c1a"], after=SHA["c3c"],
        pull_request=pull_request(title="Speed up startup", updated=10, **pr3)),
        fetched=files(("modified", "src/app/main.py"), ("added", "src/app/util.py"), ("modified", "README.md"),
                      ("added", "docs/guide.md")))
    add("pull_request", f"{REPO}#3", iso(12), 12, envelope(
        "edited", "ada-fake", number=3, changes={"title": {"from": "Speed up startup"}},
        pull_request=pull_request(title="Prism: faster startup", updated=12, **pr3)))
    add("pull_request", f"{REPO}#3", iso(40), 40, envelope("closed", "bo-fake", number=3, pull_request=pull_request(
        title="Prism: faster startup", updated=40, state="closed", merged_at=40, **pr3)))
    # pull request #4: opened (files), assigned to cy
    add("pull_request", f"{REPO}#4", iso(5), 5, envelope("opened", "bo-fake", number=4, pull_request=pull_request(
        4, "Add a startup test", "Covers the blank window.", "bo-fake", 5, head="start-test", assignees=("cy-fake",))),
        fetched=files(("added", "tests/test_main.py")))

    # reviews: an approval without text, a comment and a change request with text
    in_review = lambda number, title, minutes, **kw: pull_request(  # noqa: E731
        number, title, kw.pop("body", "Fixes #1. Also closes #7."), kw.pop("author", "ada-fake"), minutes,
        head=kw.pop("head", "fast-start"), in_review=True, **kw)
    add("pull_request_review", f"{REPO}/review/6001", iso(30), 30, envelope(
        "submitted", "bo-fake", review=review(6001, "approved", "", "bo-fake", 30, 3),
        pull_request=in_review(3, "Prism: faster startup", 30)))
    add("pull_request_review", f"{REPO}/review/6002", iso(20), 20, envelope(
        "submitted", "cy-fake", review=review(6002, "commented", "Looks fine, one nit.", "cy-fake", 20, 3),
        pull_request=in_review(3, "Prism: faster startup", 20)))
    add("pull_request_review", f"{REPO}/review/6003", iso(15), 15, envelope(
        "submitted", "ada-fake", review=review(6003, "changes_requested", "Please add a second case.", "ada-fake", 15, 4),
        pull_request=in_review(4, "Add a startup test", 15, body="Covers the blank window.", author="bo-fake",
                               head="start-test", assignees=("cy-fake",))))

    # issue comments: on issue #1, on pull request #3 (created, then edited), one deleted, one on an old PR #9
    add("issue_comment", f"{REPO}/issue-comment/5001", iso(6), 6, envelope(
        "created", "bo-fake", issue=issue(1, "Startup shows a blank window", "Steps: open the app twice.", "ada-fake", 6,
                                          assignees=("bo-fake",)),
        comment=comment(5001, "Seeing this too.", "bo-fake", 6, 6, 1)))
    pr3_as_issue = lambda minutes: issue(3, "Prism: faster startup", "Fixes #1. Also closes #7.", "ada-fake",  # noqa: E731
                                         minutes, pull={"merged_at": None})
    add("issue_comment", f"{REPO}/issue-comment/5002", iso(25), 25, envelope(
        "created", "ada-fake", issue=pr3_as_issue(25), comment=comment(5002, "Ready for review.", "ada-fake", 25, 25, 3)))
    add("issue_comment", f"{REPO}/issue-comment/5002", iso(26), 26, envelope(
        "edited", "ada-fake", changes={"body": {"from": "Ready for review."}}, issue=pr3_as_issue(26),
        comment=comment(5002, "Ready for review now.", "ada-fake", 25, 26, 3)))
    add("issue_comment", f"{REPO}/issue-comment/5003", iso(7), 7, envelope(
        "created", "cy-fake", issue=issue(1, "Startup shows a blank window", "Steps: open the app twice.", "ada-fake", 7,
                                          assignees=("bo-fake",)),
        comment=comment(5003, "SECRET-COMMENT-TEXT", "cy-fake", 7, 7, 1)))
    add("issue_comment", f"{REPO}/issue-comment/5003", f"deleted:{received(55)}", 55, envelope(
        "deleted", "cy-fake", issue=issue(1, "Startup shows a blank window", "Steps: open the app twice.", "ada-fake", 7,
                                          assignees=("bo-fake",)),
        comment=comment(5003, "SECRET-COMMENT-TEXT", "cy-fake", 7, 7, 1)), tombstone=True)
    add("issue_comment", f"{REPO}/issue-comment/5004", iso(8), 8, envelope(
        "created", "cy-fake", issue=issue(9, "Old cleanup", "Removed unused files.", "bo-fake", 8, state="closed",
                                          pull={"merged_at": "2026-09-02T10:00:00Z"}, created=-40000),
        comment=comment(5004, "Was this the one that broke the build?", "cy-fake", 8, 8, 9)))

    # review comments on #3, one a reply; a commit comment
    add("pull_request_review_comment", f"{REPO}/review-comment/7001", iso(21), 21, envelope(
        "created", "cy-fake", comment=review_comment(7001, "Could this be a constant?", "cy-fake", 21, 3),
        pull_request=in_review(3, "Prism: faster startup", 21)))
    add("pull_request_review_comment", f"{REPO}/review-comment/7002", iso(22), 22, envelope(
        "created", "ada-fake", comment=review_comment(7002, "Done.", "ada-fake", 22, 3, reply_to=7001),
        pull_request=in_review(3, "Prism: faster startup", 22)))
    commit_comment = {"url": f"{API}/comments/8001", "html_url": f"{WEB}/commit/{SHA['c1a']}#commitcomment-8001",
                      "id": 8001, "node_id": "CC_fake8001", "user": user("bo-fake"), "position": None, "line": None,
                      "path": None, "commit_id": SHA["c1a"], "created_at": iso(9), "updated_at": iso(9),
                      "author_association": "MEMBER", "body": "Nice split.", "reactions": reactions(f"{API}/comments/8001")}
    add("commit_comment", f"{REPO}/commit-comment/8001", iso(9), 9, envelope("created", "bo-fake", comment=commit_comment))

    # a push: three commits (one each by ada, dee, ada) and its ref row
    commits = [push_commit(SHA["c1a"], "Split startup into two steps\n\nKeeps main small.", "ada-fake", 3.5,
                           added=["src/app/util.py"], modified=["src/app/main.py"]),
               push_commit(SHA["c2b"], "Mention the faster start in the readme", "dee-fake", 3.7, modified=["README.md"]),
               push_commit(SHA["c3c"], "Move the guide", "ada-fake", 3.9, added=["docs/guide.md"], removed=["docs/old.md"])]
    for commit in commits:
        add("commit", f"{REPO}@{commit['id']}", commit["id"], 4, commit)
    add("ref", f"{REPO}:refs/heads/fast-start", SHA["c3c"], 4, envelope(
        None, "ada-fake", push=True, ref="refs/heads/fast-start", before=SHA["e4d"], after=SHA["c3c"], created=False,
        deleted=False, forced=False, base_ref=None, compare=f"{WEB}/compare/{SHA['e4d'][:12]}...{SHA['c3c'][:12]}",
        head_commit=commits[-1], pusher={"name": "ada-fake", "email": "ada-fake@example.invalid"}))
    # branch created and deleted, a release published then edited, a label: none has a home in the schema
    add("ref", f"{REPO}:refs/heads/scratch", f"created:{received(11)}", 11, envelope(
        None, "bo-fake", ref="scratch", ref_type="branch", master_branch="main", description="A made-up repository for tests",
        pusher_type="user"))
    add("ref", f"{REPO}:refs/heads/scratch", f"deleted:{received(13)}", 13, envelope(
        None, "bo-fake", ref="scratch", ref_type="branch", pusher_type="user"), tombstone=True)
    release = {"url": f"{API}/releases/9001", "assets_url": f"{API}/releases/9001/assets",
               "upload_url": "https://uploads.example.invalid/9001", "html_url": f"{WEB}/releases/tag/v0.1.0", "id": 9001,
               "author": user("bo-fake"), "node_id": "RE_fake9001", "tag_name": "v0.1.0", "target_commitish": "main",
               "name": "v0.1.0", "draft": False, "immutable": False, "prerelease": False, "created_at": iso(41),
               "updated_at": iso(42), "published_at": iso(42), "assets": [], "tarball_url": f"{API}/tarball/v0.1.0",
               "zipball_url": f"{API}/zipball/v0.1.0", "body": "First made-up release.", "mentions_count": 0}
    add("release", f"{REPO}/release/9001", iso(42), 42, envelope("published", "bo-fake", release=release))
    add("release", f"{REPO}/release/9001", received(43), 43, envelope(
        "edited", "bo-fake", changes={"body": {"from": "First release."}}, release=release))
    add("label", f"{REPO}/label/9101", received(1.5), 1.5, envelope("created", "ada-fake", label={
        "id": 9101, "node_id": "LA_fake9101", "url": f"{API}/labels/startup", "name": "startup", "color": "ededed",
        "default": False, "description": "Made-up label", "archived_at": None, "archived_by": None}))
    return out


# A late, older version of pull request #3 (labeled at minute 11, before the title edit and the merge)
# that arrives after the merge.
def late_pull_request_row() -> tuple:
    payload = envelope("labeled", "ada-fake", number=3, label={
        "id": 9101, "node_id": "LA_fake9101", "url": f"{API}/labels/startup", "name": "startup", "color": "ededed",
        "default": False, "description": "Made-up label"},
                       pull_request=pull_request(3, "Speed up startup", "Fixes #1. Also closes #7.", "ada-fake", 11,
                                                 head="fast-start"))
    return ("github", "pull_request", f"{REPO}#3", iso(11), at(11), {"payload": payload})


# A late edit of the deleted comment 5003, older than the delete, that arrives after it.
def late_comment_edit_row() -> tuple:
    payload = envelope("edited", "cy-fake", changes={"body": {"from": "SECRET-COMMENT-TEXT"}},
                       issue=issue(1, "Startup shows a blank window", "Steps: open the app twice.", "ada-fake", 30,
                                   assignees=("bo-fake",)),
                       comment=comment(5003, "SECRET-EDITED-TEXT", "cy-fake", 7, 30, 1))
    return ("github", "issue_comment", f"{REPO}/issue-comment/5003", iso(30), at(30), {"payload": payload})


EXTRACTED_ROWS = 25  # every row except the five ref, release and label rows
NOT_EXTRACTED = {"ref": 3, "release": 2, "label": 1}

# The graph rows() loads to (subgraph <tenant>_github), by hand from the rows above.
EXPECTED = {
    "nodes": {
        "Container": 1,  # lumen-labs/prism
        "WorkItem": 3,  # issues #1, #2 (deleted) and #7, a placeholder: PR #3 says "closes #7" and #7 never arrives
        "Change": 6,  # PRs #3, #4, #9 (only seen inside a comment's payload), commits c1a, c2b, c3c
        "Message": 9,  # issue comments 5001-5004, reviews 6002 and 6003 (6001 has no text), review comments
                       # 7001 and 7002, commit comment 8001
        "Identity": 4, "Person": 4,  # ada, bo, cy, dee (dee only authors a commit)
        "Module": 4,  # directories src/app, ".", docs, tests
    },
    "placeholders": {"WorkItem": 1},
    "relationships": {
        "Container CONTAINS WorkItem": 2,
        "Container CONTAINS Change": 6,
        "Container CONTAINS Module": 4,
        "Person AUTHORED WorkItem": 2,
        "Person ASSIGNED_TO WorkItem": 1,
        "Person AUTHORED Change": 6,
        "Person ASSIGNED_TO Change": 1,
        "Person AUTHORED Message": 9,
        "Person REVIEWED Change": 3,
        "Message REFERENCES Change": 7,  # 5002 #3, 5004 #9, 6002 #3, 6003 #4, 7001 #3, 7002 #3, 8001 c1a
        "Message REFERENCES WorkItem": 2,  # 5001 and 5003, both on #1
        "Message REPLIES_TO Message": 1,
        "Change RESOLVES WorkItem": 2,  # #3 fixes #1 and closes #7
        "Change TOUCHES Module": 7,  # #3: src/app, ".", docs; #4: tests; c1a src/app; c2b "."; c3c docs
        "Identity RESOLVES_TO Person": 4,
    },
}
