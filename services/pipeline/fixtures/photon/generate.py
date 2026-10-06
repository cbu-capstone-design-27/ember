"""Synthetic Slack workspace and Jira board for ryan-stoffel/photon.

Jira: EMBER-43. Stdlib only. Reads photon-source.json (a snapshot of the
repo's GitHub issues, pull requests, comments, and releases) and writes:

- slack-intake.jsonl, jira-intake.jsonl: EMBER-39 envelopes
- slack-intake.jsonl.readable.md, jira-intake.jsonl.readable.md: the
  connectors' own glance summaries
- slack.md, jira.md: full-text transcripts for people

The JSONL is not written by hand. The fake Slack Web API and fake Jira REST
API below answer the real pull code in services/slack-ingestion and
services/jira-ingestion, so the output is what those connectors emit.

    python3 services/pipeline/fixtures/photon/generate.py
    python3 services/pipeline/fixtures/photon/generate.py --out /tmp/photon
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
SOURCE = HERE / "photon-source.json"
GITHUB = "https://github.com/ryan-stoffel/photon"
PACIFIC = timezone(timedelta(hours=-7), "PDT")
NAMESPACE = uuid.UUID("5d3c2a9e-0b7f-4c61-9a43-7e1d2f8b6c05")

def _load(service: str, name: str):
    folder = REPO / "services" / service
    sys.modules.pop("envelope", None)
    sys.path.insert(0, str(folder))
    try:
        spec = importlib.util.spec_from_file_location(name, folder / "pull_test.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(folder))
        sys.modules.pop("envelope", None)
    return module

def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def jira_time(moment: datetime) -> str:
    return moment.astimezone(PACIFIC).strftime("%Y-%m-%dT%H:%M:%S.000-0700")

def pacific(moment: datetime) -> str:
    return moment.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M PT")

def stable_id(*parts) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(str(part) for part in parts)))

def gh_refs(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"GH-(\d+)", text, re.I)]

# People. Ryan Stoffel is the only human in the photon history. Everything
# else in it is a bot: dependabot, Cursor Bugbot, and the release workflow.

RYAN_NAME = "Ryan Stoffel"
RYAN_EMAIL = "stoffel.thomas.ryan@gmail.com"
RYAN_LOGIN = "ryan-stoffel"
RYAN_TZ = "America/Los_Angeles"

TEAM_ID = "T08PHOTON01"
TEAM = {
    "id": TEAM_ID,
    "name": "Photon",
    "url": "https://photon-synthetic.slack.com/",
    "domain": "photon-synthetic",
    "email_domain": "",
    "icon": {"image_default": True},
    "avatar_base_url": "https://ca.slack-edge.com/",
    "is_verified": False,
    "lob_sales_home_enabled": False,
    "is_sfdc_auto_slack": False,
}

U_RYAN = "U08RSTOFFEL"
U_SLACKBOT = "USLACKBOT"
U_GITHUB = "U08GITHUB01"
U_JIRA = "U08JIRACLD1"
U_EMBER = "U08EMBERBOT"

BOTS = {
    U_GITHUB: {"bot_id": "B08GITHUB01", "app_id": "A01BP7R4KNY", "name": "GitHub"},
    U_JIRA: {"bot_id": "B08JIRACLD1", "app_id": "A2RPP3NFR", "name": "Jira Cloud"},
    U_EMBER: {"bot_id": "B08EMBERBOT", "app_id": "A08EMBERAPP", "name": "Ember"},
}

WORKSPACE_CREATED = utc("2026-09-14T02:30:00Z")

def slack_user(uid: str, name: str, real: str, *, bot: bool = False, email: str | None = None, updated: datetime) -> dict:
    profile = {
        "title": "",
        "phone": "",
        "skype": "",
        "real_name": real,
        "real_name_normalized": real,
        "display_name": "" if bot else real.split()[0],
        "display_name_normalized": "" if bot else real.split()[0],
        "fields": None,
        "status_text": "",
        "status_emoji": "",
        "status_expiration": 0,
        "avatar_hash": stable_id("avatar", uid)[:12],
        "first_name": real.split()[0],
        "last_name": " ".join(real.split()[1:]),
        "team": TEAM_ID,
    }
    if email:
        profile["email"] = email
    if bot:
        profile["bot_id"] = BOTS[uid]["bot_id"]
        profile["api_app_id"] = BOTS[uid]["app_id"]
        profile["always_active"] = True
    user = {
        "id": uid,
        "team_id": TEAM_ID,
        "name": name,
        "deleted": False,
        "color": "9f69e7",
        "real_name": real,
        "tz": RYAN_TZ,
        "tz_label": "Pacific Daylight Time",
        "tz_offset": -25200,
        "profile": profile,
        "is_admin": uid == U_RYAN,
        "is_owner": uid == U_RYAN,
        "is_primary_owner": uid == U_RYAN,
        "is_restricted": False,
        "is_ultra_restricted": False,
        "is_bot": bot,
        "is_app_user": False,
        "updated": int(updated.timestamp()),
        "is_email_confirmed": uid == U_RYAN,
        "who_can_share_contact_card": "EVERYONE",
    }
    return user

def slack_users() -> list[dict]:
    slackbot = slack_user(U_SLACKBOT, "slackbot", "Slackbot", updated=WORKSPACE_CREATED)
    slackbot["is_bot"] = False
    slackbot["profile"]["display_name"] = "Slackbot"
    slackbot["profile"]["display_name_normalized"] = "Slackbot"
    return [
        slack_user(U_RYAN, "ryan", RYAN_NAME, email=RYAN_EMAIL, updated=WORKSPACE_CREATED),
        slackbot,
        slack_user(U_GITHUB, "github", "GitHub", bot=True, updated=WORKSPACE_CREATED + timedelta(minutes=12)),
        slack_user(U_JIRA, "jira_cloud", "Jira Cloud", bot=True, updated=WORKSPACE_CREATED + timedelta(minutes=14)),
        slack_user(U_EMBER, "ember", "Ember", bot=True, updated=utc("2026-10-05T16:00:00Z")),
    ]

CHANNELS = [
    ("C08GENERAL1", "general", "Workspace announcements.", True, True),
    ("C08PHOTONDV", "photon-dev", "Decisions, conventions, and working notes for Photon.", True, False),
    ("C08PHOTONGH", "photon-github", "GitHub activity for ryan-stoffel/photon.", True, False),
    ("C08PHOTONRL", "photon-releases", "Published Photon releases.", True, False),
    ("C08PHOTONBG", "photon-bugs", "Regressions and bug triage.", True, False),
    ("C08RANDOM01", "random", "Anything else.", False, False),
]
CHANNEL_ID = {name: cid for cid, name, _purpose, _member, _general in CHANNELS}

def slack_channel(cid: str, name: str, purpose: str, member: bool, general: bool, created: datetime) -> dict:
    return {
        "id": cid,
        "name": name,
        "is_channel": True,
        "is_group": False,
        "is_im": False,
        "is_mpim": False,
        "is_private": False,
        "created": int(created.timestamp()),
        "is_archived": False,
        "is_general": general,
        "unlinked": 0,
        "name_normalized": name,
        "is_shared": False,
        "is_org_shared": False,
        "is_pending_ext_shared": False,
        "pending_shared": [],
        "context_team_id": TEAM_ID,
        "updated": int(created.timestamp() * 1000),
        "parent_conversation": None,
        "creator": U_RYAN,
        "is_ext_shared": False,
        "shared_team_ids": [TEAM_ID],
        "pending_connected_team_ids": [],
        "is_member": member,
        "topic": {"value": "", "creator": "", "last_set": 0},
        "purpose": {"value": purpose, "creator": U_RYAN, "last_set": int(created.timestamp())},
        "properties": {},
        "previous_names": [],
        "num_members": 5 if member else 1,
    }

# Ryan's own messages. Times are UTC and sit where the GitHub history puts the
# work they describe. Each entry: channel, time, text, replies, reactions.
SLACK_THREADS = [
    ("general", "2026-09-14T02:31:00Z",
     "Setting this workspace up for Photon. Code is at https://github.com/ryan-stoffel/photon and tickets are on the PHO board. "
     "#photon-dev is for decisions and conventions, #photon-github and #photon-releases are bot feeds, and #photon-bugs is for regressions.",
     [], []),
    ("photon-dev", "2026-09-14T02:35:00Z",
     "Scope for Photon, written down so I stop relitigating it: applications, clipboard history, notes, file search, and keybinds. "
     "No AI, no extensions, no account, no cloud sync, no telemetry. Swift 6 with AppKit for the panel and process behavior and SwiftUI for content. macOS 14 and later.",
     [("2026-09-14T02:37:00Z", "Branching is git-flow. `develop` is the default branch, `main` only takes release merges, and `main` is back-merged into `develop` after every tag."),
      ("2026-09-14T02:38:30Z", "Branch names: `feature/GH-<issue>-<slug>` and `bug/GH-<issue>-<slug>`. CI checks the name, so a PR on a bad branch fails before build.")],
     ["pushpin"]),
    ("photon-dev", "2026-09-14T03:55:00Z",
     "Clipboard and file search both take over the launcher panel, but through two paths: clipboard uses `LauncherSession.clipboard`, files use the `LauncherMode` protocol. "
     "Folding clipboard onto `LauncherMode` is the right end state, not now. Filed GH-17 so it does not happen as a side effect of the file search PR.",
     [], []),
    ("photon-dev", "2026-09-14T04:52:00Z",
     "v0.1.0 is out, ad-hoc signed. Developer ID signing and notarization are wired into the release workflow but wait on Apple secrets (GH-11). "
     "Until then Gatekeeper needs Open Anyway on first launch.",
     [("2026-09-14T04:55:00Z", "The README has the Control-click and `xattr -dr com.apple.quarantine` workaround for now.")],
     []),
    ("photon-dev", "2026-09-14T13:26:00Z",
     "Lost a release PR to the branch-name check. Dots are not allowed in `chore/` slugs, so `chore/merge-main-into-develop-v0.1.1` fails. "
     "Use `chore/backmerge-main-0-2-0` style. Writing it here because I will forget.",
     [("2026-09-14T17:04:00Z", "Did it again with `chore/backmerge-main-0.2.0` (#60). Closed it and recreated as #61."),
      ("2026-09-14T20:22:00Z", "And again with #74. Keeping the rule. It is the right rule, I just need to stop typing dots.")],
     ["pushpin"]),
    ("photon-dev", "2026-09-14T21:30:00Z",
     "File search returned nothing from the launcher. Switched to Spotlight `mdfind` scoped to the home folder (GH-79) instead of walking the filesystem. "
     "The test case I keep using: typing `ember` should find `Ember_Individual_Pitch.pdf` in Documents.",
     [("2026-09-14T23:24:00Z", "`mdfind -onlyin $HOME` still missed that file. Added an `mdfind -name` filename fallback and a timeout so Files cannot stick on Searching (GH-91).")],
     []),
    ("photon-dev", "2026-09-15T06:53:00Z",
     "Going to try a Rust + GPUI rewrite (GH-100). The Swift UI keeps shipping clipped clipboard overlays and stuck file search, and I want layout and ranking in crates I can test. "
     "Same compact bar, same cask, no new visual language.",
     [("2026-09-15T07:02:00Z", "CI has to build this on `macos-latest`. Linux cannot compile GPUI against AppKit.")],
     []),
    ("photon-dev", "2026-09-15T14:25:00Z",
     "v0.3.0 is bad. Photon shows up in the Dock, the launcher has a title bar with traffic lights, appearance is stuck on light, the panel moves when clicked, and app icons do not render. Filing it as GH-107 with the Dock screenshot.",
     [("2026-09-15T14:34:00Z", "New rule: no release unless a parity harness launches the packaged Photon.app on `macos-latest` and checks panel style, activation policy, menu bar, appearance, frame, icons, and the `Cmd+Shift+V` clipboard session."),
      ("2026-09-15T14:36:00Z", "If GPUI cannot pass that, the v0.2.3 Swift tree comes back. I am not shipping known-broken GPUI to get the rewrite out."),
      ("2026-09-15T16:05:00Z", "Decided. GPUI did not pass. v0.3.1 restores Swift 6 + SwiftUI/AppKit as the release stack. The rewrite stays in history as 0.3.0 and nowhere else.")],
     ["pushpin"]),
    ("photon-dev", "2026-09-15T16:35:00Z",
     "Cleaned up the PR pile from the rollback. #108, #110, and #112 are closed as superseded by #109, #111, and #113. None of them had anything unique.",
     [], []),
    ("photon-dev", "2026-09-17T04:15:00Z",
     "Cursor Bugbot posts \"usage limit reached\" on every PR now. I am not paying for more usage on a solo repo. Those comments are not reviews, ignore them.",
     [], []),
    ("photon-dev", "2026-09-17T18:47:00Z",
     "GitHub renamed my account from `RyanStoffel` to `ryan-stoffel`. Repo links redirect, but Homebrew records tap trust by name, so the old `ryanstoffel/homebrew-tap` breaks `brew trust`. "
     "Docs use `ryan-stoffel/taps` everywhere now (#158).",
     [], ["pushpin"]),
    ("photon-dev", "2026-09-17T19:05:00Z",
     "Settled the panel size: 760 x 502 for launcher Suggestions, Files, and expanded clipboard. One frame for all three and no expansion animation. "
     "The parity gate compares the three frames for exact equality (GH-159).",
     [], []),
    ("photon-dev", "2026-09-17T23:45:00Z",
     "Note for later: issues do not auto-close when a release PR merges to `main`, because `develop` is the default branch. Close them by hand with a \"Shipped in vX\" comment.",
     [], ["pushpin"]),
    ("photon-dev", "2026-09-21T06:20:00Z",
     "Launched apps were not coming to the front (GH-178). Photon now hides itself first, then activates the target with `yieldActivation`. Doing the launcher perf pass on the same branch.",
     [], []),
    ("photon-dev", "2026-09-21T16:45:00Z",
     "Caps Lock as Hyper was turning Caps Lock on (GH-194). The remap now swallows the lock-state change while Hyper is held. A tap can still be set to nothing, Escape, or Caps Lock.",
     [], []),
    ("photon-dev", "2026-09-21T19:45:00Z",
     "Pinning running apps to the top of the launcher felt wrong after a day. 0.4.3 replaces it with Suggestions ranked by how often each app is opened on this Mac. The running dots stay.",
     [("2026-09-28T17:50:00Z", "Suggestions rank commands by open count too now (GH-232). Files, single notes, and Settings panes stay in the catalog below.")],
     []),
    ("photon-dev", "2026-09-22T16:00:00Z",
     "Onboarding has had three versions in two days: the 0.4.3 walkthrough, the interactive tour in 0.4.4, and the full-screen sequence in 0.4.5. The full-screen one covers everything, which is too much. 0.4.6 plays it in a window (GH-221).",
     [("2026-09-22T18:30:00Z", "Splitting onboarding into phases (GH-226). Phase 1 is the arrival. Phase 2 is the feature tour and permission prompts.")],
     []),
    ("photon-dev", "2026-09-22T19:20:00Z",
     "Running a dev build kills my release Photon because they share a bundle id and hotkeys. Adding a Photon-Dev app with its own bundle id that can run beside it (GH-228).",
     [], []),
    ("photon-dev", "2026-09-28T17:05:00Z",
     "Scrapping the cinematic Phase 1. No beam, flash, stars, full-screen veil, or Desktop 2 pin. It becomes one small native welcome window: how to open Photon, where Settings are, and one Continue. Phase 2 is not in this pass.",
     [("2026-09-28T17:09:00Z", "Sora stays in `Resources/Fonts`, but the welcome window uses the system font.")],
     ["pushpin"]),
    ("photon-dev", "2026-09-29T03:20:00Z",
     "v0.4.7 is the first notarized build. The tag workflow signed with Developer ID, notarized, stapled, and bumped the cask on its own. GH-11 is closed after two weeks.",
     [], ["tada"]),
    ("photon-dev", "2026-09-29T16:55:00Z",
     "Tried an outlined Photon mark for the menu bar (GH-243). Not the direction. The current menu bar icon stays.",
     [], []),
    ("photon-dev", "2026-10-04T02:45:00Z",
     "Audited 0.4.8 against Raycast. Root search fills with loose System Settings matches, `saf` never lists Safari, root queries flip into Files mode, there is no Cmd+K action panel, and Esc does not clear text first. "
     "The calculator crashes on `2^64` and gets `2*-3` wrong. All of it is GH-248.",
     [("2026-10-04T03:10:00Z", "Still no AI and no extensions. Parity means the core interactions, not the store.")],
     []),
    ("photon-bugs", "2026-09-14T04:37:00Z",
     "Launcher panel throws `NSInternalInconsistencyException` at launch (GH-22). `moveToActiveSpace` conflicts with the other collection behavior flags. Dropping it.",
     [("2026-09-14T04:41:00Z", "Fixed in #24.")],
     ["white_check_mark"]),
    ("photon-bugs", "2026-09-15T16:52:00Z",
     "Real-account file search is still broken after 0.3.1, and Up/Down in clipboard history does nothing on hardware. The CI harness passed both, so it was testing the wrong thing (GH-114).",
     [("2026-09-15T17:55:00Z", "0.3.2 fixes both. The harness now seeds a real Documents PDF and four pasteboard entries and checks the displayed rows through Accessibility.")],
     ["white_check_mark"]),
    ("photon-bugs", "2026-09-17T20:30:00Z",
     "Files metadata paints over the footer buttons, folder grant dialogs close the panel, and drag only works from the top edge (GH-164 through GH-167). One branch for all four.",
     [("2026-09-17T23:10:00Z", "Merged in #169. Grants are a sheet on the launcher now, queued one at a time, and they survive relaunch.")],
     ["white_check_mark"]),
    ("photon-bugs", "2026-09-28T17:25:00Z",
     "Typing `finder` shows file hits above Finder.app (GH-233). An application name match beats file hits now.",
     [], ["white_check_mark"]),
    ("photon-bugs", "2026-09-29T16:50:00Z",
     "The Settings sidebar focus ring is stuck again, this time after a click (GH-238). Same symptom as GH-202, different trigger.",
     [], []),
    ("general", "2026-10-05T16:05:00Z",
     "Added @Ember to #general, #photon-dev, #photon-github, #photon-releases, and #photon-bugs. It reads only channels it is invited to, so #random stays out.",
     [], []),
]

RELEASE_REPLIES = {
    "v0.3.0": [("2026-09-15T08:40:00Z", "Shipped the GPUI rewrite. Testing on my own machine before I trust it.")],
    "v0.3.1": [("2026-09-15T16:20:00Z", "Rollback to the v0.2.3 Swift tree. See #photon-dev for why.")],
    "v0.4.7": [("2026-09-29T03:16:00Z", "First release that is Developer ID signed and notarized.")],
}

class SlackWorld:
    """Every channel's messages, built once. Answers the fake Web API."""

    def __init__(self, source: dict) -> None:
        self.source = source
        self.users = slack_users()
        self.channels = [slack_channel(cid, name, purpose, member, general, WORKSPACE_CREATED + timedelta(seconds=i))
                         for i, (cid, name, purpose, member, general) in enumerate(CHANNELS)]
        self.messages: dict[str, list[dict]] = {cid: [] for cid, *_ in CHANNELS}
        self.replies: dict[tuple[str, str], list[dict]] = {}
        self._used: set[str] = set()
        self._build()

    def _ts(self, moment: datetime) -> str:
        micros = 100
        while True:
            ts = f"{int(moment.timestamp())}.{micros:06d}"
            if ts not in self._used:
                self._used.add(ts)
                return ts
            micros += 100

    def _message(self, uid: str, moment: datetime, text: str, **extra) -> dict:
        message = {"user": uid, "type": "message", "ts": self._ts(moment), "text": text, "team": TEAM_ID}
        if uid in BOTS:
            bot = BOTS[uid]
            message["bot_id"] = bot["bot_id"]
            message["app_id"] = bot["app_id"]
            message["bot_profile"] = {
                "id": bot["bot_id"],
                "deleted": False,
                "name": bot["name"],
                "updated": int(WORKSPACE_CREATED.timestamp()),
                "app_id": bot["app_id"],
                "team_id": TEAM_ID,
            }
        else:
            message["client_msg_id"] = stable_id("msg", message["ts"])
            message["blocks"] = [{
                "type": "rich_text",
                "block_id": stable_id("block", message["ts"])[:5],
                "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}],
            }]
        message.update(extra)
        return message

    def _thread(self, channel: str, parent: dict, replies: list[tuple[str, str, str]], reactions: list[str]) -> None:
        cid = CHANNEL_ID[channel]
        if reactions:
            parent["reactions"] = [{"name": name, "users": [U_RYAN], "count": 1} for name in reactions]
        self.messages[cid].append(parent)
        if not replies:
            return
        built = []
        for uid, when, text in replies:
            reply = self._message(uid, utc(when), text, thread_ts=parent["ts"], parent_user_id=parent["user"])
            built.append(reply)
        users = sorted({reply["user"] for reply in built})
        parent.update(
            thread_ts=parent["ts"],
            reply_count=len(built),
            reply_users_count=len(users),
            latest_reply=built[-1]["ts"],
            reply_users=users,
            is_locked=False,
            subscribed=parent["user"] == U_RYAN,
        )
        self.replies[(cid, parent["ts"])] = built

    def _build(self) -> None:
        for channel, when, text, replies, reactions in SLACK_THREADS:
            parent = self._message(U_RYAN, utc(when), text)
            self._thread(channel, parent, [(U_RYAN, w, t) for w, t in replies], reactions)

        for issue in self.source["issues"]:
            link = f"<{GITHUB}/issues/{issue['number']}|#{issue['number']} {issue['title']}>"
            parent = self._message(U_GITHUB, utc(issue["created_at"]), f"Issue opened by {RYAN_LOGIN}: {link}")
            replies = []
            if issue["closed_at"]:
                verb = "closed as not planned" if issue["state_reason"] == "not_planned" else "closed"
                replies.append((U_GITHUB, issue["closed_at"], f"Issue {verb} by {RYAN_LOGIN}: {link}"))
            self._thread("photon-github", parent, replies, [])

        for pull in self.source["pulls"]:
            link = f"<{GITHUB}/pull/{pull['number']}|#{pull['number']} {pull['title']}>"
            branches = f"`{pull['head']}` into `{pull['base']}`"
            parent = self._message(U_GITHUB, utc(pull["created_at"]), f"Pull request opened by {pull['author']}: {link} ({branches})")
            replies = []
            if pull["merged_at"]:
                replies.append((U_GITHUB, pull["merged_at"], f"Pull request merged by {RYAN_LOGIN}: {link}"))
            elif pull["closed_at"]:
                replies.append((U_GITHUB, pull["closed_at"], f"Pull request closed by {RYAN_LOGIN}: {link}"))
            self._thread("photon-github", parent, replies, [])

        for release in self.source["releases"]:
            url = f"{GITHUB}/releases/tag/{release['tag']}"
            text = f"Release published: <{url}|{release['name']}> (pre-release). {release['summary']}".rstrip()
            parent = self._message(U_GITHUB, utc(release["published_at"]), text)
            replies = [(U_RYAN, w, t) for w, t in RELEASE_REPLIES.get(release["tag"], [])]
            self._thread("photon-releases", parent, replies, [])

    def bug_created(self, key: str, summary: str, created: datetime, url: str) -> None:
        text = f"{RYAN_NAME} created Bug <{url}|{key}: {summary}>"
        self._thread("photon-bugs", self._message(U_JIRA, created + timedelta(seconds=20), text), [], [])

    def finish(self) -> None:
        for cid in self.messages:
            self.messages[cid].sort(key=lambda m: float(m["ts"]), reverse=True)

    def answer(self, method: str, params: dict) -> dict:
        page = {"ok": True, "response_metadata": {"next_cursor": ""}}
        if method == "team.info":
            return {"ok": True, "team": TEAM}
        if method == "users.list":
            return dict(page, members=self.users, cache_ts=int(utc("2026-10-05T16:30:00Z").timestamp()))
        if method == "conversations.list":
            return dict(page, channels=self.channels)
        if method == "conversations.history":
            return dict(page, messages=self.messages[params["channel"]], has_more=False, pin_count=0)
        if method == "conversations.replies":
            parent = next(m for m in self.messages[params["channel"]] if m["ts"] == params["ts"])
            return dict(page, messages=[parent] + self.replies[(params["channel"], params["ts"])], has_more=False)
        return {"ok": False, "error": "unknown_method"}

def slack_exchange(world: SlackWorld, module):
    def exchange(method: str, url: str, headers: dict, body):
        parts = urlsplit(url)
        api_method = parts.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        payload = world.answer(api_method, params)
        return module.Response(200, {}, json.dumps(payload).encode("utf-8"))

    return exchange

ORIGIN = "https://photon-synthetic.atlassian.net"
API = f"{ORIGIN}/rest/api/3"
PROJECT_KEY = "PHO"
PROJECT_ID = "10000"
PROJECT_CREATED = utc("2026-09-14T02:40:00Z")
RYAN_ACCOUNT = "712020:" + stable_id("jira", RYAN_EMAIL)

RYAN_JIRA = {
    "self": f"{API}/user?accountId={RYAN_ACCOUNT}",
    "accountId": RYAN_ACCOUNT,
    "emailAddress": RYAN_EMAIL,
    "avatarUrls": {size: f"https://secure.gravatar.com/avatar/{stable_id('gravatar', RYAN_EMAIL)[:32]}?s={size[:2]}" for size in ("48x48", "24x24", "16x16", "32x32")},
    "displayName": RYAN_NAME,
    "active": True,
    "timeZone": RYAN_TZ,
    "accountType": "atlassian",
}

STATUS = {
    "To Do": {"id": "10000", "category": {"id": 2, "key": "new", "colorName": "blue-gray", "name": "To Do"}},
    "In Progress": {"id": "10001", "category": {"id": 4, "key": "indeterminate", "colorName": "yellow", "name": "In Progress"}},
    "Done": {"id": "10002", "category": {"id": 3, "key": "done", "colorName": "green", "name": "Done"}},
}
ISSUE_TYPES = {
    "Epic": {"id": "10001", "hierarchyLevel": 1, "description": "Epics track collections of related bugs, stories, and tasks."},
    "Story": {"id": "10004", "hierarchyLevel": 0, "description": "Stories track functionality or features expressed as user goals."},
    "Bug": {"id": "10005", "hierarchyLevel": 0, "description": "Bugs track problems or errors."},
    "Task": {"id": "10006", "hierarchyLevel": 0, "description": "Tasks track small, distinct pieces of work."},
}
PRIORITIES = {"p0": ("1", "Highest"), "p1": ("2", "High"), "p2": ("3", "Medium"), "p3": ("4", "Low")}
RESOLUTIONS = {"Done": "10000", "Won't Do": "10001"}

EPICS = [
    ("Launcher", "2026-09-14T02:42:00Z", "area/launcher", "Panel, search, ranking, Suggestions, drag and snap."),
    ("Clipboard history", "2026-09-14T02:42:30Z", "area/clipboard", "Clipboard capture, history UI, and paste-back."),
    ("Notes", "2026-09-14T02:43:00Z", "area/notes", "Floating markdown notes."),
    ("File search", "2026-09-14T02:43:30Z", "area/files", "Spotlight file search, Files mode, folder access."),
    ("Keybinds and window management", "2026-09-14T02:44:00Z", "area/keybinds", "Hyper key, app shortcuts, window commands."),
    ("Settings", "2026-09-14T02:44:30Z", "area/settings", "Settings window and preferences."),
    ("Release and CI", "2026-09-14T02:45:00Z", "area/release", "CI, parity harness, signing, Homebrew cask, releases."),
    ("Onboarding", "2026-09-21T18:30:00Z", "onboarding", "First-run experience and permission prompts."),
]
AREA_TO_EPIC = {"area/ci": "Release and CI"}

SPRINTS = [
    (1, "PHO Sprint 1", "2026-09-14T02:46:00Z", "2026-09-21T02:46:00Z", "closed"),
    (2, "PHO Sprint 2", "2026-09-21T02:46:00Z", "2026-09-28T02:46:00Z", "closed"),
    (3, "PHO Sprint 3", "2026-09-28T02:46:00Z", "2026-10-05T02:46:00Z", "closed"),
    (4, "PHO Sprint 4", "2026-10-05T02:46:00Z", "2026-10-12T02:46:00Z", "active"),
]

JIRA_NOTES = {
    17: [("2026-09-14T04:05:00Z", "Deferred on purpose. Do this as its own change, not inside a file search fix. Clipboard stays on LauncherSession until then.")],
    100: [("2026-09-15T06:58:00Z", "Why: the Swift UI keeps regressing clipboard overlays and file search, and I want layout and ranking in crates I can test. If the port cannot match the v0.2.3 compact stills, it does not ship.")],
    107: [("2026-09-15T14:40:00Z", "Parity gate before any further release: packaged Photon.app on macos-latest, borderless non-activating panels, no Dock icon, live appearance, stable frame, bundle icons, and the clipboard hotkey session."),
          ("2026-09-15T16:06:00Z", "Decision: GPUI failed the gate. v0.3.1 restores the v0.2.3 Swift tree and keeps the harness as a required release check.")],
    226: [("2026-09-22T18:32:00Z", "Phase 1 is the arrival. Phase 2 is the feature tour and permission prompts.")],
    248: [("2026-10-04T03:12:00Z", "Scope stays the same: no AI and no extensions. This is about root search ranking, Esc order, an action panel, the calculator, and clipboard reliability.")],
}

# (outward GitHub number, link type, inward GitHub number). Relates is symmetric.
LINKS = [
    (100, "Relates", 107),
    (3, "Relates", 17),
    (5, "Relates", 17),
    (129, "Blocks", 134),
    (130, "Blocks", 134),
    (131, "Blocks", 134),
    (132, "Blocks", 134),
    (141, "Blocks", 145),
    (142, "Blocks", 145),
    (143, "Blocks", 145),
    (216, "Relates", 226),
    (221, "Relates", 226),
    (242, "Relates", 226),
    (202, "Relates", 238),
]
LINK_TYPES = {
    "Relates": {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
    "Blocks": {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
}

ATTACHMENTS = {
    107: [("v0.3.0-dock-regression.png", "image/png", 412_388, "2026-09-15T14:32:30Z")],
    164: [("files-metadata-over-footer.png", "image/png", 288_904, "2026-09-17T20:28:40Z")],
}

def text_node(text: str) -> list[dict]:
    nodes: list[dict] = []
    for index, chunk in enumerate(re.split(r"`([^`]+)`", text)):
        if not chunk:
            continue
        node = {"type": "text", "text": chunk}
        if index % 2:
            node["marks"] = [{"type": "code"}]
        nodes.append(node)
    return nodes

def adf(markdown: str) -> dict:
    """Small markdown to Atlassian document converter: headings, lists, code, paragraphs."""
    content: list[dict] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    paragraph: list[str] = []
    bullets: list[str] = []
    ordered: list[str] = []

    def flush() -> None:
        if paragraph:
            content.append({"type": "paragraph", "content": text_node(" ".join(paragraph))})
            paragraph.clear()
        for items, kind in ((bullets, "bulletList"), (ordered, "orderedList")):
            if items:
                content.append({"type": kind, "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": text_node(item)}]} for item in items
                ]})
                items.clear()

    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            content.append({"type": "codeBlock", "attrs": {}, "content": [{"type": "text", "text": "\n".join(code)}]})
        elif not stripped or stripped.startswith("<!--"):
            flush()
        elif heading := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush()
            content.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": text_node(heading.group(2))})
        elif item := re.match(r"^[-*]\s+(?:\[[ xX]\]\s+)?(.*)$", stripped):
            if paragraph or ordered:
                flush()
            bullets.append(item.group(1))
        elif item := re.match(r"^\d+\.\s+(.*)$", stripped):
            if paragraph or bullets:
                flush()
            ordered.append(item.group(1))
        else:
            if bullets or ordered:
                flush()
            paragraph.append(stripped)
        index += 1
    flush()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}

def issue_type(issue: dict) -> str:
    labels = issue["labels"]
    title = issue["title"].lower()
    if "type/bug" in labels or title.startswith("fix") or title.startswith("bug"):
        return "Bug"
    if "type/feature" in labels or "enhancement" in labels or title.startswith("feat"):
        return "Story"
    if any(label in labels for label in ("type/infra", "type/chore")) or "release" in title:
        return "Task"
    return "Story"

def epic_for(issue: dict) -> str | None:
    title = issue["title"].lower()
    if any(word in title for word in ("onboarding", "walkthrough", "first-run", "welcome", "first launch")):
        return "Onboarding"
    for label in issue["labels"]:
        if label in AREA_TO_EPIC:
            return AREA_TO_EPIC[label]
        for name, _created, area, _summary in EPICS:
            if label == area:
                return name
    for word, name in (("clipboard", "Clipboard history"), ("file", "File search"), ("notes", "Notes"),
                       ("release", "Release and CI"), ("settings", "Settings"), ("launcher", "Launcher"),
                       ("calculator", "Launcher"), ("photon-dev", "Release and CI"), ("rust", "Launcher"),
                       ("menu bar", "Launcher"), ("suggestions", "Launcher")):
        if word in title:
            return name
    return None

def sprint_for(moment: datetime) -> int:
    for number, _name, start, end, _state in SPRINTS:
        if utc(start) <= moment < utc(end):
            return number
    return SPRINTS[-1][0]

class JiraWorld:
    """Project, versions, and issues, built once. Answers the fake REST API."""

    def __init__(self, source: dict, slack: SlackWorld) -> None:
        self.source = source
        self.issues: dict[str, dict] = {}
        self.extra: dict[str, dict] = {}
        self.order: list[str] = []
        self._ids = iter(range(10100, 99999))
        self._build(slack)

    def _next(self) -> str:
        return str(next(self._ids))

    def _versions(self) -> None:
        self.versions = []
        for index, release in enumerate(self.source["releases"]):
            vid = str(10010 + index)
            local = utc(release["published_at"]).astimezone(PACIFIC)
            self.versions.append({
                "self": f"{API}/version/{vid}",
                "id": vid,
                "description": release["summary"],
                "name": release["tag"][1:],
                "archived": False,
                "released": True,
                "releaseDate": local.strftime("%Y-%m-%d"),
                "userReleaseDate": f"{local.day}/{local:%b/%y}",
                "projectId": int(PROJECT_ID),
                "_published": utc(release["published_at"]),
            })

    def _fix_version(self, closed: datetime) -> dict | None:
        for version in self.versions:
            if version["_published"] >= closed:
                return {k: v for k, v in version.items() if not k.startswith("_") and k != "userReleaseDate"}
        return None

    def _sprint(self, number: int) -> dict:
        _n, name, start, end, state = SPRINTS[number - 1]
        sprint = {"id": number, "name": name, "state": state, "boardId": 1,
                  "goal": "", "startDate": iso_z(utc(start)), "endDate": iso_z(utc(end))}
        if state == "closed":
            sprint["completeDate"] = iso_z(utc(end) - timedelta(minutes=1))
        return sprint

    def _plan(self) -> list[dict]:
        plans = []
        for name, created, _area, summary in EPICS:
            plans.append({"epic": name, "created": utc(created), "summary": name, "description": summary})
        for issue in self.source["issues"]:
            plans.append({"github": issue, "created": utc(issue["created_at"])})
        plans.sort(key=lambda plan: (plan["created"], plan.get("github", {}).get("number", 0)))
        for number, plan in enumerate(plans, start=1):
            plan["key"] = f"{PROJECT_KEY}-{number}"
            plan["id"] = str(10000 + number)
        return plans

    def _base(self, plan: dict, kind: str, summary: str, description: dict, status: str) -> dict:
        type_info = ISSUE_TYPES[kind]
        status_info = STATUS[status]
        return {
            "expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
            "id": plan["id"],
            "self": f"{API}/issue/{plan['id']}",
            "key": plan["key"],
            "fields": {
                "summary": summary,
                "description": description,
                "issuetype": {
                    "self": f"{API}/issuetype/{type_info['id']}",
                    "id": type_info["id"],
                    "description": type_info["description"],
                    "name": kind,
                    "subtask": False,
                    "hierarchyLevel": type_info["hierarchyLevel"],
                },
                "project": {
                    "self": f"{API}/project/{PROJECT_ID}",
                    "id": PROJECT_ID,
                    "key": PROJECT_KEY,
                    "name": "Photon",
                    "projectTypeKey": "software",
                    "simplified": True,
                },
                "status": {
                    "self": f"{API}/status/{status_info['id']}",
                    "description": "",
                    "name": status,
                    "id": status_info["id"],
                    "statusCategory": dict(status_info["category"], self=f"{API}/statuscategory/{status_info['category']['id']}"),
                },
                "creator": RYAN_JIRA,
                "reporter": RYAN_JIRA,
                "assignee": RYAN_JIRA,
                "created": jira_time(plan["created"]),
                "labels": [],
                "components": [],
                "fixVersions": [],
                "issuelinks": [],
                "attachment": [],
                "subtasks": [],
                "resolution": None,
                "resolutiondate": None,
                "statuscategorychangedate": jira_time(plan["created"]),
                "watches": {"self": f"{API}/issue/{plan['key']}/watchers", "watchCount": 1, "isWatching": True},
                "votes": {"self": f"{API}/issue/{plan['key']}/votes", "votes": 0, "hasVoted": False},
                "worklog": {"startAt": 0, "maxResults": 20, "total": 0, "worklogs": []},
                "timetracking": {},
                "customfield_10020": None,
            },
        }

    def _comment(self, issue_id: str, when: datetime, text: str) -> dict:
        cid = self._next()
        return {
            "self": f"{API}/issue/{issue_id}/comment/{cid}",
            "id": cid,
            "author": RYAN_JIRA,
            "body": adf(text),
            "updateAuthor": RYAN_JIRA,
            "created": jira_time(when),
            "updated": jira_time(when),
            "jsdPublic": True,
        }

    def _history(self, when: datetime, items: list[dict]) -> dict:
        return {"id": self._next(), "author": RYAN_JIRA, "created": jira_time(when), "items": items}

    def _build(self, slack: SlackWorld) -> None:
        self._versions()
        plans = self._plan()
        epic_ref: dict[str, dict] = {}
        by_number: dict[int, dict] = {}
        pulls_for: dict[int, list[dict]] = {}
        for pull in self.source["pulls"]:
            for number in set(gh_refs(pull["head"])):
                pulls_for.setdefault(number, []).append(pull)
        comments_for: dict[int, list[dict]] = {}
        for comment in self.source["comments"]:
            comments_for.setdefault(comment["issue"], []).append(comment)

        for plan in plans:
            if "epic" in plan:
                issue = self._base(plan, "Epic", plan["summary"], adf(plan["description"]), "To Do")
                issue["fields"]["labels"] = ["epic"]
                issue["fields"]["updated"] = jira_time(plan["created"])
                epic_ref[plan["epic"]] = issue
                self.extra[plan["key"]] = {"comments": [], "histories": [], "remote": [], "attachments": []}
            else:
                issue = self._issue(plan, epic_ref, pulls_for, comments_for, slack)
                by_number[plan["github"]["number"]] = issue
            self.issues[plan["key"]] = issue
            self.order.append(plan["key"])

        self._link(by_number)
        self._close_epics(epic_ref)

    def _issue(self, plan, epic_ref, pulls_for, comments_for, slack) -> dict:
        gh = plan["github"]
        number = gh["number"]
        kind = issue_type(gh)
        created = plan["created"]
        closed = utc(gh["closed_at"]) if gh["closed_at"] else None
        pulls = sorted(pulls_for.get(number, []), key=lambda pull: pull["created_at"])
        started = utc(pulls[0]["created_at"]) if pulls else None
        if closed and (started is None or started > closed):
            started = min(created + timedelta(minutes=2), closed)
        if closed:
            status = "Done"
        elif started:
            status = "In Progress"
        else:
            status = "To Do"

        description = adf(gh["body"]) if gh["body"].strip() else adf(gh["title"])
        issue = self._base(plan, kind, gh["title"], description, status)
        fields = issue["fields"]
        priority = next((PRIORITIES[label.split("/", 1)[1]] for label in gh["labels"] if label.startswith("priority/")), PRIORITIES["p2"])
        fields["priority"] = {"self": f"{API}/priority/{priority[0]}", "iconUrl": f"{ORIGIN}/images/icons/priorities/{priority[1].lower()}_new.svg", "name": priority[1], "id": priority[0]}
        fields["labels"] = sorted({"github"} | {label.split("/", 1)[1] for label in gh["labels"] if label.startswith("area/")})
        epic_name = epic_for(gh)
        if epic_name:
            epic = epic_ref[epic_name]
            fields["parent"] = {
                "id": epic["id"],
                "key": epic["key"],
                "self": epic["self"],
                "fields": {
                    "summary": epic["fields"]["summary"],
                    "status": epic["fields"]["status"],
                    "priority": {"self": f"{API}/priority/3", "name": "Medium", "id": "3"},
                    "issuetype": epic["fields"]["issuetype"],
                },
            }
        if not (status == "To Do" and number == 17):
            fields["customfield_10020"] = [self._sprint(sprint_for(created))]

        histories = [self._history(created + timedelta(seconds=30), [
            {"field": "Sprint", "fieldtype": "custom", "fieldId": "customfield_10020", "from": "", "fromString": "",
             "to": str(sprint_for(created)), "toString": SPRINTS[sprint_for(created) - 1][1]}
        ])] if fields["customfield_10020"] else []
        if started:
            histories.append(self._history(started, [
                {"field": "status", "fieldtype": "jira", "fieldId": "status", "from": STATUS["To Do"]["id"], "fromString": "To Do",
                 "to": STATUS["In Progress"]["id"], "toString": "In Progress"}
            ]))
        updated = started or created
        if closed:
            resolution = "Won't Do" if gh["state_reason"] == "not_planned" else "Done"
            fields["resolution"] = {"self": f"{API}/resolution/{RESOLUTIONS[resolution]}", "id": RESOLUTIONS[resolution], "description": "", "name": resolution}
            fields["resolutiondate"] = jira_time(closed)
            fields["statuscategorychangedate"] = jira_time(closed)
            items = [
                {"field": "resolution", "fieldtype": "jira", "fieldId": "resolution", "from": None, "fromString": None,
                 "to": RESOLUTIONS[resolution], "toString": resolution},
                {"field": "status", "fieldtype": "jira", "fieldId": "status", "from": STATUS["In Progress"]["id"], "fromString": "In Progress",
                 "to": STATUS["Done"]["id"], "toString": "Done"},
            ]
            version = self._fix_version(closed) if resolution == "Done" else None
            if version:
                fields["fixVersions"] = [version]
                items.append({"field": "Fix Version", "fieldtype": "jira", "fieldId": "fixVersions", "from": None, "fromString": None,
                              "to": version["id"], "toString": version["name"]})
            histories.append(self._history(closed, items))
            updated = closed
        elif started:
            fields["statuscategorychangedate"] = jira_time(started)

        comments = []
        notes = [(utc(c["created_at"]), c["body"]) for c in comments_for.get(number, [])]
        notes += [(utc(when), text) for when, text in JIRA_NOTES.get(number, [])]
        for when, text in sorted(notes):
            comments.append(self._comment(plan["id"], when, text))
            updated = max(updated, when)
        fields["comment"] = {"comments": comments, "self": f"{API}/issue/{plan['id']}/comment", "maxResults": len(comments), "total": len(comments), "startAt": 0}

        attachments = []
        for filename, mime, size, when in ATTACHMENTS.get(number, []):
            att = self._next()
            attachments.append({
                "self": f"{API}/attachment/{att}",
                "id": att,
                "filename": filename,
                "author": RYAN_JIRA,
                "created": jira_time(utc(when)),
                "size": size,
                "mimeType": mime,
                "content": f"{API}/attachment/content/{att}",
                "thumbnail": f"{API}/attachment/thumbnail/{att}",
            })
        fields["attachment"] = attachments

        remote = [self._remote(plan["key"], f"{GITHUB}/issues/{number}", f"ryan-stoffel/photon#{number}: {gh['title']}",
                               "issue", closed is not None)]
        for pull in pulls:
            remote.append(self._remote(plan["key"], f"{GITHUB}/pull/{pull['number']}", f"ryan-stoffel/photon#{pull['number']}: {pull['title']}",
                                       "pull request", pull["merged_at"] is not None or pull["closed_at"] is not None))
        fields["updated"] = jira_time(updated)
        self.extra[plan["key"]] = {"comments": comments, "histories": histories, "remote": remote, "attachments": attachments}
        if kind == "Bug":
            slack.bug_created(plan["key"], gh["title"], created, f"{ORIGIN}/browse/{plan['key']}")
        return issue

    def _remote(self, key: str, url: str, title: str, what: str, resolved: bool) -> dict:
        rid = self._next()
        return {
            "id": int(rid),
            "self": f"{API}/issue/{key}/remotelink/{rid}",
            "globalId": f"github={url}",
            "application": {"type": "com.github", "name": "GitHub"},
            "relationship": "mentioned in" if what == "pull request" else "GitHub issue",
            "object": {
                "url": url,
                "title": title,
                "icon": {"url16x16": "https://github.com/favicon.ico", "title": "GitHub"},
                "status": {"resolved": resolved, "icon": {}},
            },
        }

    def _link(self, by_number: dict[int, dict]) -> None:
        for outward, name, inward in LINKS:
            source, target = by_number[outward], by_number[inward]
            lid = self._next()
            link_type = dict(LINK_TYPES[name], self=f"{API}/issueLinkType/{LINK_TYPES[name]['id']}")

            def brief(issue: dict) -> dict:
                fields = issue["fields"]
                return {"id": issue["id"], "key": issue["key"], "self": issue["self"], "fields": {
                    "summary": fields["summary"], "status": fields["status"], "priority": fields["priority"], "issuetype": fields["issuetype"]}}

            source["fields"]["issuelinks"].append({"id": lid, "self": f"{API}/issueLink/{lid}", "type": link_type, "outwardIssue": brief(target)})
            target["fields"]["issuelinks"].append({"id": lid, "self": f"{API}/issueLink/{lid}", "type": link_type, "inwardIssue": brief(source)})

    def _close_epics(self, epic_ref: dict[str, dict]) -> None:
        for name, epic in epic_ref.items():
            children = [issue for issue in self.issues.values() if issue["fields"].get("parent", {}).get("key") == epic["key"]]
            open_children = [c for c in children if c["fields"]["status"]["name"] != "Done"]
            fields = epic["fields"]
            fields["priority"] = {"self": f"{API}/priority/3", "iconUrl": f"{ORIGIN}/images/icons/priorities/medium_new.svg", "name": "Medium", "id": "3"}
            histories = self.extra[epic["key"]]["histories"]
            created = datetime.strptime(fields["created"], "%Y-%m-%dT%H:%M:%S.000%z")
            first = min((datetime.strptime(c["fields"]["created"], "%Y-%m-%dT%H:%M:%S.000%z") for c in children), default=None)
            if first:
                histories.append(self._history(max(first, created) + timedelta(minutes=1), [
                    {"field": "status", "fieldtype": "jira", "fieldId": "status", "from": STATUS["To Do"]["id"], "fromString": "To Do",
                     "to": STATUS["In Progress"]["id"], "toString": "In Progress"}]))
                fields["status"] = self._status("In Progress")
                fields["updated"] = jira_time(max(first, created) + timedelta(minutes=1))
            if children and not open_children:
                last = max(datetime.strptime(c["fields"]["resolutiondate"], "%Y-%m-%dT%H:%M:%S.000%z") for c in children)
                done = last + timedelta(minutes=5)
                histories.append(self._history(done, [
                    {"field": "resolution", "fieldtype": "jira", "fieldId": "resolution", "from": None, "fromString": None, "to": "10000", "toString": "Done"},
                    {"field": "status", "fieldtype": "jira", "fieldId": "status", "from": STATUS["In Progress"]["id"], "fromString": "In Progress",
                     "to": STATUS["Done"]["id"], "toString": "Done"}]))
                fields["status"] = self._status("Done")
                fields["resolution"] = {"self": f"{API}/resolution/10000", "id": "10000", "description": "", "name": "Done"}
                fields["resolutiondate"] = jira_time(done)
                fields["updated"] = jira_time(done)
            fields["comment"] = {"comments": [], "self": f"{API}/issue/{epic['id']}/comment", "maxResults": 0, "total": 0, "startAt": 0}

    def _status(self, name: str) -> dict:
        info = STATUS[name]
        return {"self": f"{API}/status/{info['id']}", "description": "", "name": name, "id": info["id"],
                "statusCategory": dict(info["category"], self=f"{API}/statuscategory/{info['category']['id']}")}

    def project(self) -> dict:
        return {
            "expand": "description,lead,issueTypes,url,projectKeys,permissions,insight",
            "self": f"{API}/project/{PROJECT_ID}",
            "id": PROJECT_ID,
            "key": PROJECT_KEY,
            "description": "Jira board for Photon, a minimal macOS launcher. Code: https://github.com/ryan-stoffel/photon",
            "lead": RYAN_JIRA,
            "components": [],
            "issueTypes": [{"self": f"{API}/issuetype/{info['id']}", "id": info["id"], "name": name, "subtask": False,
                            "hierarchyLevel": info["hierarchyLevel"]} for name, info in ISSUE_TYPES.items()],
            "versions": [],
            "name": "Photon",
            "roles": {"Administrator": f"{API}/project/{PROJECT_ID}/role/10002", "Member": f"{API}/project/{PROJECT_ID}/role/10003"},
            "projectTypeKey": "software",
            "simplified": True,
            "style": "next-gen",
            "isPrivate": False,
            "properties": {},
            "entityId": stable_id("project", PROJECT_KEY),
            "uuid": stable_id("project", PROJECT_KEY),
        }

    def statuses(self) -> list[dict]:
        return [{
            "self": f"{API}/issuetype/{info['id']}",
            "id": info["id"],
            "name": name,
            "subtask": False,
            "statuses": [self._status(status) for status in STATUS],
        } for name, info in ISSUE_TYPES.items()]

    def answer(self, method: str, url: str, body) -> tuple[int, object]:
        parts = urlsplit(url)
        path = parts.path.removeprefix("/rest/api/3")
        if method == "POST" and path == "/search/jql":
            return 200, {"issues": [{"id": self.issues[key]["id"], "key": key} for key in self.order], "isLast": True}
        if path == f"/project/{PROJECT_KEY}":
            return 200, self.project()
        if path == f"/project/{PROJECT_KEY}/components":
            return 200, []
        if path == f"/project/{PROJECT_KEY}/statuses":
            return 200, self.statuses()
        if path == f"/project/{PROJECT_KEY}/properties":
            return 200, {"keys": []}
        if path == f"/project/{PROJECT_KEY}/version":
            values = [{k: v for k, v in version.items() if not k.startswith("_")} for version in self.versions]
            return 200, {"self": url, "maxResults": 100, "startAt": 0, "total": len(values), "isLast": True, "values": values}
        if match := re.fullmatch(r"/attachment/(\d+)", path):
            for extra in self.extra.values():
                for att in extra["attachments"]:
                    if att["id"] == match.group(1):
                        return 200, att
            return 404, {"errorMessages": ["not found"]}
        match = re.fullmatch(r"/issue/([A-Z]+-\d+)(?:/(\w+))?", path)
        if not match or match.group(1) not in self.issues:
            return 404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]}
        key, tail = match.group(1), match.group(2)
        issue, extra = self.issues[key], self.extra[key]
        if tail is None:
            return 200, issue
        if tail == "comment":
            comments = extra["comments"]
            return 200, {"startAt": 0, "maxResults": 100, "total": len(comments), "comments": comments}
        if tail == "changelog":
            values = extra["histories"]
            return 200, {"self": url, "maxResults": 100, "startAt": 0, "total": len(values), "isLast": True, "values": values}
        if tail == "remotelink":
            return 200, extra["remote"]
        if tail == "watchers":
            return 200, {"self": f"{API}/issue/{key}/watchers", "isWatching": True, "watchCount": 1, "watchers": [RYAN_JIRA]}
        if tail == "votes":
            return 200, {"self": f"{API}/issue/{key}/votes", "votes": 0, "hasVoted": False, "voters": []}
        if tail == "properties":
            return 200, {"keys": []}
        return 404, {"errorMessages": ["not found"]}

def jira_exchange(world: JiraWorld, module):
    def exchange(method: str, url: str, headers: dict, body):
        status, payload = world.answer(method, url, body)
        return module.Response(status, {}, json.dumps(payload).encode("utf-8"))

    return exchange

def slack_markdown(envelopes: list[dict]) -> str:
    bodies = [env["body"] for env in envelopes]
    names = {b["id"]: b.get("real_name") or b["name"] for b in bodies if "profile" in b and "is_bot" in b}
    channels = [b for b in bodies if b.get("is_channel")]
    team = bodies[0]
    lines = [f"# Slack: {team['name']} (synthetic)", "",
             f"Workspace `{team['id']}` at {team['url']}. Generated for EMBER-43 from the ryan-stoffel/photon history. "
             "Times are Pacific. Thread replies are indented under their parent.", "",
             "## Members", ""]
    for b in bodies:
        if "profile" in b and "is_bot" in b:
            kind = "bot" if b["is_bot"] else "person"
            email = f", {b['profile']['email']}" if b["profile"].get("email") else ""
            lines.append(f"- {names[b['id']]} (`{b['id']}`, {kind}{email})")
    for channel in channels:
        messages = [b for b in bodies if b.get("type") == "message" and b.get("channel") == channel["id"]]
        lines += ["", f"## #{channel['name']}", "", f"_{channel['purpose']['value']}_ {len(messages)} messages.", ""]
        for m in messages:
            when = pacific(datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc))
            text = m["text"].replace("\n", " ")
            reacts = "".join(f" :{r['name']}:" for r in m.get("reactions", []))
            if m.get("thread_ts") not in (None, m["ts"]):
                lines.append(f"    - **{names.get(m['user'], m['user'])}** {when}: {text}")
            else:
                lines.append(f"- **{names.get(m['user'], m['user'])}** {when}: {text}{reacts}")
    return "\n".join(lines).rstrip() + "\n"

def adf_markdown(node, depth: int = 0) -> str:
    if isinstance(node, list):
        return "".join(adf_markdown(child, depth) for child in node)
    kind = node.get("type")
    if kind == "text":
        text = node["text"]
        return f"`{text}`" if any(mark["type"] == "code" for mark in node.get("marks", [])) else text
    inner = adf_markdown(node.get("content", []), depth + 1)
    if kind == "heading":
        return f"**{inner}**\n\n"
    if kind == "paragraph":
        return f"{inner}\n\n" if depth <= 1 else inner
    if kind in ("bulletList", "orderedList"):
        return "".join(f"- {adf_markdown(item.get('content', []), depth + 2).strip()}\n" for item in node["content"]) + "\n"
    if kind == "codeBlock":
        return f"```\n{inner}\n```\n\n"
    return inner

def jira_markdown(envelopes: list[dict], module) -> str:
    bodies = [env["body"] for env in envelopes]
    issues = [b for b in bodies if module.classify(b) == "issue"]
    comments: dict[str, list[dict]] = {}
    histories: dict[str, list[dict]] = {}
    remote: dict[str, list[dict]] = {}
    current = None
    for b in bodies:
        kind = module.classify(b)
        if kind == "issue":
            current = b["key"]
        elif kind == "comment":
            comments.setdefault(current, []).append(b)
        elif kind == "changelog":
            histories.setdefault(current, []).extend(b["values"])
        elif kind == "remote_link":
            remote.setdefault(current, []).append(b)
    versions = [b for b in bodies if module.classify(b) == "version"]
    epics = [i for i in issues if i["fields"]["issuetype"]["name"] == "Epic"]

    lines = ["# Jira: Photon (PHO, synthetic)", "",
             f"Site {ORIGIN}. Generated for EMBER-43 from the ryan-stoffel/photon history. "
             "Every non-epic issue mirrors one GitHub issue; its remote links name that issue and the pull requests whose branch carries its number. Times are Pacific.", "",
             "## Sprints", ""]
    for number, name, start, end, state in SPRINTS:
        lines.append(f"- {name}: {pacific(utc(start))} to {pacific(utc(end))} ({state})")
    lines += ["", "## Versions", ""]
    for v in versions:
        lines.append(f"- {v['name']} released {v['releaseDate']}: {v['description']}")

    def render(issue: dict) -> list[str]:
        f = issue["fields"]
        sprint = ", ".join(s["name"] for s in f.get("customfield_10020") or []) or "backlog"
        fix = ", ".join(v["name"] for v in f["fixVersions"]) or "none"
        resolution = f" ({f['resolution']['name']})" if f.get("resolution") else ""
        out = ["", f"### {issue['key']}: {f['summary']}", "",
               f"{f['issuetype']['name']}, {f['status']['name']}{resolution}, priority {f['priority']['name']}. "
               f"Sprint: {sprint}. Fix version: {fix}. Labels: {', '.join(f['labels']) or 'none'}.", "",
               f"Created {pacific(datetime.strptime(f['created'], '%Y-%m-%dT%H:%M:%S.000%z'))} by {f['reporter']['displayName']}, assigned to {f['assignee']['displayName']}."]
        if f.get("resolutiondate"):
            out[-1] += f" Resolved {pacific(datetime.strptime(f['resolutiondate'], '%Y-%m-%dT%H:%M:%S.000%z'))}."
        for link in f["issuelinks"]:
            if "outwardIssue" in link:
                out.append(f"- {link['type']['outward']} {link['outwardIssue']['key']}")
            else:
                out.append(f"- {link['type']['inward']} {link['inwardIssue']['key']}")
        for link in remote.get(issue["key"], []):
            out.append(f"- GitHub: [{link['object']['title']}]({link['object']['url']})")
        for att in f["attachment"]:
            out.append(f"- Attachment: {att['filename']} ({att['mimeType']}, {att['size']} bytes)")
        description = adf_markdown(f["description"]).strip()
        if description:
            out += ["", description]
        for c in comments.get(issue["key"], []):
            when = pacific(datetime.strptime(c["created"], "%Y-%m-%dT%H:%M:%S.000%z"))
            out += ["", f"> **{c['author']['displayName']}** {when}: " + adf_markdown(c["body"]).strip().replace("\n\n", " ").replace("\n", " ")]
        changes = []
        for h in histories.get(issue["key"], []):
            when = pacific(datetime.strptime(h["created"], "%Y-%m-%dT%H:%M:%S.000%z"))
            bits = [f"{item['field']} {item['fromString'] + ' to ' if item.get('fromString') else ''}{item['toString']}" for item in h["items"]]
            changes.append(f"{when}: {'; '.join(bits)}")
        if changes:
            out += ["", "History: " + " | ".join(changes)]
        return out

    for epic in epics:
        children = [i for i in issues if i["fields"].get("parent", {}).get("key") == epic["key"]]
        lines += ["", f"## Epic {epic['key']}: {epic['fields']['summary']} ({len(children)} issues)"]
        lines += render(epic)
        for child in children:
            lines += render(child)
    orphans = [i for i in issues if i["fields"]["issuetype"]["name"] != "Epic" and "parent" not in i["fields"]]
    if orphans:
        lines += ["", f"## No epic ({len(orphans)} issues)"]
        for issue in orphans:
            lines += render(issue)
    return "\n".join(lines).rstrip() + "\n"

def write_jsonl(path: Path, envelopes: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for envelope in envelopes:
            stream.write(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n")

def generate(out: Path) -> dict[str, int]:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    slack_pull = _load("slack-ingestion", "slack_pull")
    jira_pull = _load("jira-ingestion", "jira_pull")

    slack = SlackWorld(source)
    jira = JiraWorld(source, slack)
    slack.finish()

    client = slack_pull.make_client("xoxb-synthetic", slack_exchange(slack, slack_pull), gates=slack_pull.MethodGates())
    slack_envelopes = [slack_pull.wrap_slack(body) for body in slack_pull.collect(client, workers=1)]
    jira_envelopes = list(jira_pull.pull(ORIGIN, "synthetic@example.com", "synthetic-token", PROJECT_KEY,
                                         jira_exchange(jira, jira_pull), workers=1))

    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "slack-intake.jsonl", slack_envelopes)
    write_jsonl(out / "jira-intake.jsonl", jira_envelopes)
    (out / "slack-intake.jsonl.readable.md").write_text(slack_pull.render_summary(slack_envelopes), encoding="utf-8")
    (out / "jira-intake.jsonl.readable.md").write_text(jira_pull.render_summary(PROJECT_KEY, jira_envelopes), encoding="utf-8")
    (out / "slack.md").write_text(slack_markdown(slack_envelopes), encoding="utf-8")
    (out / "jira.md").write_text(jira_markdown(jira_envelopes, jira_pull), encoding="utf-8")
    return {"slack": len(slack_envelopes), "jira": len(jira_envelopes)}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic photon Slack and Jira fixtures.")
    parser.add_argument("--out", type=Path, default=HERE, help="Output folder. Default: next to this script.")
    args = parser.parse_args(argv)
    counts = generate(args.out)
    print(f"slack {counts['slack']} envelopes, jira {counts['jira']} envelopes -> {args.out}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
