# Jira: Photon (PHO, synthetic)

Site https://photon-synthetic.atlassian.net. Generated for EMBER-43 from the ryan-stoffel/photon history. Every non-epic issue mirrors one GitHub issue; its remote links name that issue and the pull requests whose branch carries its number. Times are Pacific.

## Sprints

- PHO Sprint 1: 2026-09-13 19:46 PT to 2026-09-20 19:46 PT (closed)
- PHO Sprint 2: 2026-09-20 19:46 PT to 2026-09-27 19:46 PT (closed)
- PHO Sprint 3: 2026-09-27 19:46 PT to 2026-10-04 19:46 PT (closed)
- PHO Sprint 4: 2026-10-04 19:46 PT to 2026-10-11 19:46 PT (active)

## Versions

- 0.1.0 released 2026-09-13: First public build. Photon is a menu-bar launcher for macOS 14 and later; it has no Dock icon, no account, no cloud sync, and no telemetry.
- 0.1.1 released 2026-09-14: UI polish release: redesigned launcher, Appearance settings, real app icons, Notes sidebar, and a screenshot harness for visual QA on macOS.
- 0.2.0 released 2026-09-14: Feature release: inline calculator and unit conversions, launcher drag-and-snap positioning, clipboard and file-search UX aligned with the redesigned launcher, and searchable System Settings pane titles.
- 0.2.1 released 2026-09-14: Patch release: launcher polish, home-scoped file search, compact clipboard hotkey panel, and drag guide fixes.
- 0.2.2 released 2026-09-14: Patch release: clipboard arrow navigation, search-field mode pills removed, live launcher drag, and Spotlight `mdfind` file search.
- 0.2.3 released 2026-09-14: Patch release: clipboard reopen/arrows, live-snap drag, Documents file search, and Down-to-recents.
- 0.3.0 released 2026-09-15: Rust + GPUI rewrite. Superseded by v0.3.1 because the shipped app regressed native macOS panel, Dock/menu, appearance, positioning, icon, clipboard, and feature behavior.
- 0.3.1 released 2026-09-15: Emergency rollback release: restore the v0.2.3 Swift/AppKit implementation after the v0.3.0 Rust/GPUI rewrite failed the native macOS parity gate.
- 0.3.2 released 2026-09-15: Runtime reliability release for real-account file search and clipboard keyboard navigation.
- 0.3.3 released 2026-09-15: Workflow completion release for clipboard paste-back, guided file access, launcher dragging, and expanded Clipboard and Files detail views.
- 0.3.4 released 2026-09-16: Regression fix release for Ryan's v0.3.3 file search, drag guides, main-bar Files layout, and clipboard Enter paste.
- 0.3.5 released 2026-09-16: Ryan UX and real-world file search release.
- 0.3.6 released 2026-09-17: Ryan layout, animation, clipboard selection, and smoke-harness release.
- 0.3.7 released 2026-09-17: Ryan shared-panel sizing and instant expansion release.
- 0.3.8 released 2026-09-17: Ryan Files footer, folder grants, panel drag, and center-snap release.
- 0.3.9 released 2026-09-17: Ryan Notes chrome and launcher recs-scroll release.
- 0.4.0 released 2026-09-21: Ryan foreground launch, snappy launcher, native Settings, and macOS 26/27 chrome release.
- 0.4.1 released 2026-09-21: Ryan Dock-style running dots and trailing keybind chips release.
- 0.4.2 released 2026-09-21: Ryan Photon-styled Settings, Caps Lock Hyper, app-hotkeys list, and running-apps-first release.
- 0.4.3 released 2026-09-21: Ryan most-used Suggestions, Settings keyboard focus, first-launch permissions, and a short walkthrough.
- 0.4.4 released 2026-09-21: Ryan interactive first-run tour and full app icon release.
- 0.4.5 released 2026-09-22: Ryan cinematic full-screen onboarding.
- 0.4.6 released 2026-09-22: Ryan windowed first-run sequence.
- 0.4.7 released 2026-09-28: Ryan minimal first-run welcome.
- 0.4.8 released 2026-09-29: Ryan welcome window, compact footer, and Settings click focus.

## Epic PHO-1: Launcher (48 issues)

### PHO-1: Launcher

Epic, In Progress, priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:42 PT by Ryan Stoffel, assigned to Ryan Stoffel.

Panel, search, ranking, Suggestions, drag and snap.

History: 2026-09-13 19:51 PT: status To Do to In Progress

### PHO-8: Launcher hotkey (Cmd+Space) with Spotlight conflict guidance

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: github, launcher.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 19:56 PT.
- GitHub: [ryan-stoffel/photon#1: Launcher hotkey (Cmd+Space) with Spotlight conflict guidance](https://github.com/ryan-stoffel/photon/issues/1)
- GitHub: [ryan-stoffel/photon#15: feat(launcher): add hotkey, panel, app provider, and settings](https://github.com/ryan-stoffel/photon/pull/15)

**Summary**

Photon opens from a global hotkey. The default is Cmd+Space. Detect a Spotlight shortcut collision on first launch and tell the user how to disable Spotlight's shortcut. The hotkey is configurable from Settings > General.

**Acceptance criteria**

- Cmd+Space (or the configured combo) toggles the launcher while Photon is running as a menu-bar agent
- The hotkey is registered through a `HotkeyManager` that wraps Carbon `RegisterEventHotKey`
- First launch detects `com.apple.symbolichotkeys` id 64 matching Photon's shortcut and shows guidance
- Settings > General can record a new shortcut and the manager re-registers it

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-13 19:56 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-9: Launch applications via fuzzy search and frecency

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: github, launcher.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 19:56 PT.
- GitHub: [ryan-stoffel/photon#2: Launch applications via fuzzy search and frecency](https://github.com/ryan-stoffel/photon/issues/2)

**Summary**

The launcher indexes applications and System Settings panes, ranks them with fuzzy matching plus frecency, and launches the selection with `NSWorkspace`.

**Acceptance criteria**

- Indexes `/Applications`, `/System/Applications`, `~/Applications`, and `.prefPane` bundles
- Fuzzy matcher is case-insensitive subsequence search with unit tests
- Frecency store persists under Application Support and has unit tests
- Enter / click launches via `NSWorkspace`
- Results appear in the floating launcher panel with keyboard navigation

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 19:52 PT: status To Do to In Progress | 2026-09-13 19:56 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-19: Unify clipboard session with LauncherMode protocol

Task, To Do, priority Low. Sprint: backlog. Fix version: none. Labels: clipboard, github, launcher.

Created 2026-09-13 20:53 PT by Ryan Stoffel, assigned to Ryan Stoffel.
- relates to PHO-10
- relates to PHO-12
- GitHub: [ryan-stoffel/photon#17: Unify clipboard session with LauncherMode protocol](https://github.com/ryan-stoffel/photon/issues/17)

**Summary**

Clipboard history (GH-3) and file search (GH-5) both take over the launcher panel, but they do it through two parallel mechanisms:

- Clipboard: `LauncherSession.clipboard` plus `ClipboardHistoryViewModel` (`attachClipboard`, `cb ` / `clipboard ` prefixes, Esc back to the command list).
- File search: the generic `LauncherMode` protocol (`Sources/Photon/Launcher/LauncherMode.swift`) plus `FileSearchMode` (`/`, `f `, Search Files command, Quick Look, inline results).

They work side by side on purpose. Do not fold clipboard onto `LauncherMode` as part of GH-5.

**Acceptance criteria**

- Clipboard is a `LauncherMode` (or the session enum goes away) so prefixes, badges, Esc/Backspace, and results views share one path
- Existing clipboard behaviour is unchanged: `Cmd+Shift+V`, `cb ` / `clipboard `, paste/copy/pin/delete, Accessibility fallback
- File search still uses the same protocol (no one-off special case)
- Notes/keybinds can register a mode the same way if they need one

**Notes**

`LauncherViewModel` currently switches on `session` first, then `activeMode`. `LauncherPanelController` key routing does the same. The protocol already has the hooks clipboard would need (`activate`/`update`/`handle`/`makeResultsView`/`performPrimaryAction`).

> **Ryan Stoffel** 2026-09-13 21:05 PT: Deferred on purpose. Do this as its own change, not inside a file search fix. Clipboard stays on LauncherSession until then.

### PHO-20: Launcher panel throws NSInternalInconsistencyException at launch

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: github, launcher.

Created 2026-09-13 21:34 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 21:40 PT.
- GitHub: [ryan-stoffel/photon#22: Launcher panel throws NSInternalInconsistencyException at launch](https://github.com/ryan-stoffel/photon/issues/22)
- GitHub: [ryan-stoffel/photon#24: fix(launcher): drop the conflicting moveToActiveSpace panel behaviour](https://github.com/ryan-stoffel/photon/pull/24)

**Summary**

The release dry run's smoke test log ([run 34805534226](https://github.com/RyanStoffel/photon/actions/runs/34805534226), artifact `smoke-test-log`) shows AppKit raising an uncaught exception while `LauncherPanelController.makePanel()` runs from `AppRuntime.start()`:

```
*** Assertion failure in -[NSWindow setCollectionBehavior:], NSWindow.m:14727
window behavior cannot be both NSWindowCollectionBehaviorCanJoinAllSpaces and NSWindowCollectionBehaviorMoveToActiveSpace
```

`Sources/Photon/Launcher/LauncherPanelController.swift` sets `panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .moveToActiveSpace]`. The two Spaces flags are mutually exclusive.

**Impact**

AppKit swallows the exception on the main run loop, so the process survives (the smoke test passed), but `AppRuntime.start()` aborts at `launcher.preload()`. Nothing after it runs: no launcher hotkey, no clipboard monitor, no notes hotkey, no Spotlight conflict guidance. On a real Mac the app would show a menu bar item and do nothing else.

**Fix**

- Drop `.moveToActiveSpace` (the launcher wants to be visible on every Space, which is `.canJoinAllSpaces`).
- Make `Scripts/smoke-test.sh` fail when the unified log records `An uncaught exception was raised` or an `HIExceptions` fault, so this class of bug turns CI red.

History: 2026-09-13 21:35 PT: Sprint PHO Sprint 1 | 2026-09-13 21:38 PT: status To Do to In Progress | 2026-09-13 21:40 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-21: Launcher shows a placeholder square instead of app icons

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: github, launcher.

Created 2026-09-13 22:34 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 22:53 PT.
- GitHub: [ryan-stoffel/photon#29: Launcher shows a placeholder square instead of app icons](https://github.com/ryan-stoffel/photon/issues/29)
- GitHub: [ryan-stoffel/photon#31: fix(launcher): show real icons for apps, panes, and provider rows](https://github.com/ryan-stoffel/photon/pull/31)

**Summary**

Every application and System Settings pane row in the launcher shows the same grey placeholder square instead of the app's icon. Feedback from the v0.1.0 test on real hardware: "right now none of the app icons work".

**Cause**

`LauncherView.resultIcon(for:)` only asks the active `LauncherMode` (file search) for an `NSImage`. Application, pane, clipboard, notes, and keybind commands have no icon source at all, so every one of them falls through to a provider SF Symbol; for `apps` that symbol is `app.fill`, the filled square in the screenshot. `AppsProvider` never calls `NSWorkspace.shared.icon(forFile:)` and nothing is prefetched or cached for app rows.

**Fix**

- Give `Command` an optional, platform-neutral `icon` (`CommandIcon`: Finder icon of a path, an image file, an application by bundle id, or an SF Symbol).
- `PhotonApps` sets it: apps use the bundle's Finder icon; System Settings panes use their declared pane icon and fall back to the System Settings app icon when a pane declares none.
- Clipboard, notes, files, and keybinds set sensible icons for their rows.
- The launcher resolves icons through one cache (`NSWorkspace.shared.icon(forFile:)`, `NSImage(contentsOf:)`, `urlForApplication(withBundleIdentifier:)`), warms it after the app index loads, and renders `Image(nsImage:)` resizable at a fixed size.

**Acceptance**

- App rows show the real app icon; pane rows show a pane or System Settings icon; no row shows the placeholder square.
- Unit tests cover the pure pane icon policy and the `Command` icon field.
- CI green, including `smoke`. Visual QA on a Mac follows.

History: 2026-09-13 22:35 PT: Sprint PHO Sprint 1 | 2026-09-13 22:41 PT: status To Do to In Progress | 2026-09-13 22:53 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-22: Launcher visual redesign: search field, icon rows, compact by default, Appearance settings

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: github, launcher, settings.

Created 2026-09-13 22:34 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 23:45 PT.
- GitHub: [ryan-stoffel/photon#30: Launcher visual redesign: search field, icon rows, compact by default, Appearance settings](https://github.com/ryan-stoffel/photon/issues/30)
- GitHub: [ryan-stoffel/photon#33: feat(launcher): redesign the panel, compact by default, Appearance settings](https://github.com/ryan-stoffel/photon/pull/33)

**Summary**

Functionality of v0.1.0 is right; the launcher UI is not. Feedback from the first test on real hardware:

- The search bar "looks kind of boring"; a look similar to Raycast (not a copy) is preferred.
- App rows should show only the app name and the icon. No path.
- Do not show rows until the user starts typing; the panel should be compact by default, with a settings page to adjust that.
- Replace the "Search applications" placeholder; Photon does more than apps.

**Scope**

**Search field and panel**

- Larger search text (about 20 pt), generous padding, no clutter.
- Panel about 740 pt wide, 12 pt corner radius, translucent system material through `NSVisualEffectView` (`.behindWindow`), hairline border, soft shadow. Light and dark appearance both correct.
- Slim footer: app name on the left, key hints on the right ("Open ↵"; "Actions ⌘K" only once an actions menu exists).

**Rows**

- Icon (about 28 pt) plus name for apps; no path. Subtitles only where they carry meaning (file parent path, command descriptions), rendered secondary.
- Compact row height (about 40 pt), rounded selection highlight, no per-row provider icon noise.

**Compact by default**

- Empty query shows only the search field and footer. Results expand as the user types with no perceptible delay.
- Setting to show suggestions (frecency-ranked apps and commands) before typing instead.

**Placeholder**

- Short text that says Photon does more than apps.

**Settings > Appearance (new tab)**

- Compact mode / show suggestions before typing, panel width (Compact / Regular / Wide), appearance (System / Light / Dark). Minimal, native form bound to `SettingsStore`.

**Docs**

- README text that describes the old UI, and a `CHANGELOG.md` entry under Unreleased.

**Acceptance**

- Unit tests for the pure parts (row model mapping, width presets, panel height rules).
- CI green, including `smoke`. Visual QA on a Mac follows.

Depends on the app icon fix (placeholder icons).

History: 2026-09-13 22:35 PT: Sprint PHO Sprint 1 | 2026-09-13 22:54 PT: status To Do to In Progress | 2026-09-13 23:45 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-26: feat: System Settings panes searchable by human title

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.0. Labels: github, launcher.

Created 2026-09-14 07:48 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 07:57 PT.
- GitHub: [ryan-stoffel/photon#45: feat: System Settings panes searchable by human title](https://github.com/ryan-stoffel/photon/issues/45)
- GitHub: [ryan-stoffel/photon#48: feat(apps): searchable System Settings pane titles](https://github.com/ryan-stoffel/photon/pull/48)

**Summary**

Searching the launcher for System Settings panes should work by the human-facing pane title (e.g. `wallpaper` opens Wallpaper, `Privacy & Security` opens that pane). Today many rows show internal bundle filenames such as `SharingPref` or `EnergySaverPref` because Photon only reads unlocalized Info.plist keys.

**Acceptance criteria**

- Index display names from each `.prefPane` bundle using `CFBundleDisplayName`, `CFBundleName`, and localized `InfoPlist.strings` (via the bundle’s localized info dictionary).
- Add searchable aliases for common queries (wallpaper, privacy, bluetooth, battery, etc.) without changing the visible row title.
- Launcher rows show the pane title only, with “System Settings” as secondary text when needed, and the real pane icon policy unchanged.
- Fuzzy matching considers the display title and aliases, not only the bundle identifier.
- Unit tests cover display-name resolution and alias matching using fixture pane plist data.
- CHANGELOG updated under Unreleased.

**Notes**

macOS 14+ target. Workers validate on GitHub Actions `macos-latest`.

History: 2026-09-14 07:49 PT: Sprint PHO Sprint 1 | 2026-09-14 07:49 PT: status To Do to In Progress | 2026-09-14 07:57 PT: resolution Done; status In Progress to Done; Fix Version 0.2.0

### PHO-27: Inline calculator and unit conversions in launcher

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.0. Labels: github.

Created 2026-09-14 07:48 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 08:27 PT.
- GitHub: [ryan-stoffel/photon#46: Inline calculator and unit conversions in launcher](https://github.com/ryan-stoffel/photon/issues/46)
- GitHub: [ryan-stoffel/photon#51: feat(launcher): inline calculator and unit conversions](https://github.com/ryan-stoffel/photon/pull/51)

Raycast-style inline calculator: typing expressions like `30/5` shows a result row; Enter copies the result. Support + - * / ^ % parentheses and decimals. Unit conversions for length, mass, temperature, time, and data (e.g. `10 km to mi`, `32 f to c`). Local parser in PhotonCore (unit testable), no AI.

History: 2026-09-14 07:49 PT: Sprint PHO Sprint 1 | 2026-09-14 07:50 PT: status To Do to In Progress | 2026-09-14 08:27 PT: resolution Done; status In Progress to Done; Fix Version 0.2.0

### PHO-30: Launcher: drag to reposition with center snap guides

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.0. Labels: github.

Created 2026-09-14 07:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 08:54 PT.
- GitHub: [ryan-stoffel/photon#50: Launcher: drag to reposition with center snap guides](https://github.com/ryan-stoffel/photon/issues/50)
- GitHub: [ryan-stoffel/photon#54: Launcher: drag to reposition with center snap guides](https://github.com/ryan-stoffel/photon/pull/54)

**Summary**

Allow repositioning the launcher panel vertically (and horizontally when not snapped) by click-dragging the search bar while the launcher hotkey is held.

**Interaction**

- While holding the launcher open hotkey, click and drag on the search bar to move the panel.
- While dragging, show two vertical dotted guides marking the horizontal center of the screen so the user can align the bar to center.
- When dropped within the snap zone between the guides, auto-snap to horizontal center (Y position is preserved).
- Persist chosen position in SettingsStore; next open uses saved position.
- Add "Reset launcher position to center" in Appearance settings.

**Constraints**

- Native feel, no jank.
- Must not fight the existing compact-resize-from-top behavior (top-anchored height changes).

**Acceptance**

- Drag + guides + snap behavior
- Persistence + reset control
- Unit tests for snap geometry
- CHANGELOG Unreleased entry

**Reference**

User feedback screenshot in project store: launcher-vs-raycast.png (vertical center guides mockup).

History: 2026-09-14 07:50 PT: Sprint PHO Sprint 1 | 2026-09-14 07:52 PT: status To Do to In Progress | 2026-09-14 08:54 PT: resolution Done; status In Progress to Done; Fix Version 0.2.0

### PHO-31: Raycast-style inline calculator result card

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.0. Labels: github.

Created 2026-09-14 09:26 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 09:49 PT.
- GitHub: [ryan-stoffel/photon#56: Raycast-style inline calculator result card](https://github.com/ryan-stoffel/photon/issues/56)
- GitHub: [ryan-stoffel/photon#57: Raycast-style inline calculator result card](https://github.com/ryan-stoffel/photon/pull/57)

Replace calculator list row with Raycast-like split card.

History: 2026-09-14 09:27 PT: Sprint PHO Sprint 1 | 2026-09-14 09:27 PT: status To Do to In Progress | 2026-09-14 09:49 PT: resolution Done; status In Progress to Done; Fix Version 0.2.0

### PHO-32: Calculator section sits too close to search hairline

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.1. Labels: github.

Created 2026-09-14 11:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 11:26 PT.
- GitHub: [ryan-stoffel/photon#62: Calculator section sits too close to search hairline](https://github.com/ryan-stoffel/photon/issues/62)
- GitHub: [ryan-stoffel/photon#66: fix(launcher): calculator section spacing (GH-62)](https://github.com/ryan-stoffel/photon/pull/66)

v0.2.1 bug fix

History: 2026-09-14 11:20 PT: Sprint PHO Sprint 1 | 2026-09-14 11:20 PT: status To Do to In Progress | 2026-09-14 11:26 PT: resolution Done; status In Progress to Done; Fix Version 0.2.1

### PHO-35: Launcher drag guides and reliability

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.1. Labels: github.

Created 2026-09-14 11:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 12:43 PT.
- GitHub: [ryan-stoffel/photon#65: Launcher drag guides and reliability](https://github.com/ryan-stoffel/photon/issues/65)
- GitHub: [ryan-stoffel/photon#69: fix(launcher): drag guides and file compact empty (GH-65)](https://github.com/ryan-stoffel/photon/pull/69)
- GitHub: [ryan-stoffel/photon#72: fix(launcher): file mode compact panel on enter](https://github.com/ryan-stoffel/photon/pull/72)

v0.2.1 bug fix

History: 2026-09-14 11:20 PT: Sprint PHO Sprint 1 | 2026-09-14 11:32 PT: status To Do to In Progress | 2026-09-14 12:43 PT: resolution Done; status In Progress to Done; Fix Version 0.2.1

### PHO-36: Clipboard history: arrow keys do not cycle items

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.2. Labels: clipboard, github, launcher.

Created 2026-09-14 14:16 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 14:34 PT.
- GitHub: [ryan-stoffel/photon#76: Clipboard history: arrow keys do not cycle items](https://github.com/ryan-stoffel/photon/issues/76)
- GitHub: [ryan-stoffel/photon#80: fix(clipboard): move history with arrow keys](https://github.com/ryan-stoffel/photon/pull/80)

**Summary**

In clipboard history (Cmd+Shift+V or `cb `), Down/Up do not move through previous copies. The compact bar never expands, so there is no way to cycle past the current pasteboard.

v0.2.1 (Ryan, while at work).

**Expected**

- Empty history stays a compact search bar (footer still says Clipboard).
- Typing or Down expands the list like the main launcher.
- Down/Up (and typical list navigation) move through clipboard items, wrapping at the ends.

**Actual**

Arrow keys do nothing useful. The list does not expand and selection does not move.

**Notes**

`ClipboardHistoryViewModel.handleKeyDown` consumes Up/Down, but expansion lives on `LauncherViewModel.moveSelection` (`clipboardShowsResults`). The panel controller never calls the view model path, so the first Down never opens the list.

History: 2026-09-14 14:16 PT: Sprint PHO Sprint 1 | 2026-09-14 14:17 PT: status To Do to In Progress | 2026-09-14 14:34 PT: resolution Done; status In Progress to Done; Fix Version 0.2.2

### PHO-37: Remove Clipboard/Files mode pill from the search field

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.2. Labels: clipboard, files, github, launcher.

Created 2026-09-14 14:16 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 14:25 PT.
- GitHub: [ryan-stoffel/photon#77: Remove Clipboard/Files mode pill from the search field](https://github.com/ryan-stoffel/photon/issues/77)
- GitHub: [ryan-stoffel/photon#81: fix(launcher): drop Clipboard and Files search-field pills](https://github.com/ryan-stoffel/photon/pull/81)

**Summary**

Clipboard and Files modes still show a blue capsule in the search field (\"Clipboard\" / \"Files\"). Ryan asked that pill to be removed; the mode name belongs only in the bottom footer corner.

v0.2.1 visual QA.

**Expected**

- Search field is placeholder + text only. No mode capsule.
- Compact and expanded Clipboard/Files footers keep the small corner label.

**Actual**

A blue Capsule badge sits in front of the TextField (`LauncherView.sessionBadge`).

History: 2026-09-14 14:16 PT: Sprint PHO Sprint 1 | 2026-09-14 14:18 PT: status To Do to In Progress | 2026-09-14 14:25 PT: resolution Done; status In Progress to Done; Fix Version 0.2.2

### PHO-38: Launcher drag jitters and does not follow the pointer

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.2. Labels: github, launcher.

Created 2026-09-14 14:16 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 14:42 PT.
- GitHub: [ryan-stoffel/photon#78: Launcher drag jitters and does not follow the pointer](https://github.com/ryan-stoffel/photon/issues/78)
- GitHub: [ryan-stoffel/photon#82: fix(launcher): track live drag in screen space](https://github.com/ryan-stoffel/photon/pull/82)

**Summary**

Drag guides are in the right place after 0.2.1, but live drag jitters, barely moves, and fights the mouse. The panel must track the pointer smoothly, persist Y, and snap when the panel center sits between the edge guides.

v0.2.1 (Ryan).

**Expected**

- Live drag uses screen-space mouse deltas (not SwiftUI view-local translation on a moving panel).
- No 1px jitter; the panel follows the cursor until mouse-up.
- Vertical position is stored. Horizontal snap when the panel midpoint is between the two edge guides.

**Actual**

SwiftUI `DragGesture` translation is in the search bar's local coordinates. Moving the `NSPanel` under the cursor resets that translation, so the window fights the mouse and barely travels.

History: 2026-09-14 14:16 PT: Sprint PHO Sprint 1 | 2026-09-14 14:20 PT: status To Do to In Progress | 2026-09-14 14:42 PT: resolution Done; status In Progress to Done; Fix Version 0.2.2

### PHO-40: Files empty panel stays tall instead of compact

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.2. Labels: files, github, launcher.

Created 2026-09-14 14:48 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 14:52 PT.
- GitHub: [ryan-stoffel/photon#84: Files empty panel stays tall instead of compact](https://github.com/ryan-stoffel/photon/issues/84)
- GitHub: [ryan-stoffel/photon#85: fix(files): compact empty Files panel](https://github.com/ryan-stoffel/photon/pull/85)

**Summary**

Empty Files mode is supposed to match the compact clipboard bar until the user types. v0.2.2 screenshots still show the tall idle empty state.

`FileSearchMode.prefersCompactLauncherLayout` is ignored: the property lives only on a protocol extension, so `any LauncherMode` always sees `false`.

**Expected**

Empty Files is search field + footer (Files label). Typing expands results.

**Actual**

Full-height “Search your files” empty state on enter.

History: 2026-09-14 14:48 PT: Sprint PHO Sprint 1 | 2026-09-14 14:48 PT: status To Do to In Progress | 2026-09-14 14:52 PT: resolution Done; status In Progress to Done; Fix Version 0.2.2

### PHO-41: Clipboard reopen shows clipped overlay; arrows must expand and cycle

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.2.3. Labels: clipboard, github, launcher.

Created 2026-09-14 16:06 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 16:21 PT.
- GitHub: [ryan-stoffel/photon#89: Clipboard reopen shows clipped overlay; arrows must expand and cycle](https://github.com/ryan-stoffel/photon/issues/89)
- GitHub: [ryan-stoffel/photon#94: fix(clipboard): reset compact panel on dismiss and cycle with arrows](https://github.com/ryan-stoffel/photon/pull/94)

On v0.2.2, the compact clipboard bar is correct when first opened empty. After typing, closing, or opening clipboard from the main search, the panel comes back as a clipped bar inside a huge dimmed overlay. Arrow keys also fail to expand the list and cycle items after the user has typed.

Hardware screenshots (Ryan, 2026-09-14):

- Cmd+Shift+V after typing/close/reopen: clipped chrome + huge dark overlay
- Clipboard from main search: same broken overlay, two Image rows, search field clipped off-screen

**Expected**

- Empty compact bar stays compact.
- Down expands and shows history. Up/Down cycle items even after typing. No mouse required.
- Closing and reopening (Cmd+Shift+V or from main search) restores the compact Photon bar. No huge dimmed overlay, no clipped chrome.
- Dismiss resets panel size and mode so the next open is compact unless there are results to show.

**Actual**

Reopen keeps a tall visual-effect panel. SwiftUI content is compact and vertically centered (or the top chrome is off-screen), which is the overlay/clip in the screenshots. Arrow navigation still does not reliably expand or move after typing.

**Notes**

Likely causes:

- `hide()` / `resetForShow()` do not reset `clipboardShowsResults` or force the panel back to `searchOnly` height, so the next open can keep a tall `NSPanel` while SwiftUI lays out the compact bar (NSHostingView centers the smaller view).
- `enterClipboard` sets `session = .clipboard` before `clipboardShowsResults = false`, so the first `updateContent` can resize tall, then fail to recover.
- First Down expands but returns without moving; after typing, the search field can still steal arrows if the local monitor path is skipped.

History: 2026-09-14 16:07 PT: Sprint PHO Sprint 1 | 2026-09-14 16:12 PT: status To Do to In Progress | 2026-09-14 16:21 PT: resolution Done; status In Progress to Done; Fix Version 0.2.3

### PHO-42: Launcher drag should live-snap X between the edge guides

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.3. Labels: github, launcher.

Created 2026-09-14 16:07 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 16:17 PT.
- GitHub: [ryan-stoffel/photon#90: Launcher drag should live-snap X between the edge guides](https://github.com/ryan-stoffel/photon/issues/90)
- GitHub: [ryan-stoffel/photon#93: fix(launcher): live-snap drag X between edge guides](https://github.com/ryan-stoffel/photon/pull/93)

Live tracking during a launcher drag is good on v0.2.2. Horizontal placement still drifts with small X movement, so the panel does not sit between the two dotted edge guides while the user is mostly moving vertically to set height/Y.

**Expected**

- While dragging, snap horizontally so the panel sits between the two dotted edge guides (centered in X).
- The user can drag mostly vertically to set bar height/Y; X stays in the guide corridor unless they pull clearly outside it.
- On mouse-up, keep the existing outside-corridor free placement so the panel can still be parked off-center.

**Actual**

`originByMouseDelta` follows both axes 1:1. Snap to center only runs in `finishLiveDrag` / `resolveHorizontalSnap`. Slight horizontal jitter takes the bar out of the corridor during the drag.

History: 2026-09-14 16:07 PT: Sprint PHO Sprint 1 | 2026-09-14 16:10 PT: status To Do to In Progress | 2026-09-14 16:17 PT: resolution Done; status In Progress to Done; Fix Version 0.2.3

### PHO-43: Files: ember misses Documents PDFs; Searching sticks; mix into main launcher

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.2.3. Labels: files, github, launcher.

Created 2026-09-14 16:07 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 16:23 PT.
- GitHub: [ryan-stoffel/photon#91: Files: ember misses Documents PDFs; Searching sticks; mix into main launcher](https://github.com/ryan-stoffel/photon/issues/91)
- GitHub: [ryan-stoffel/photon#95: fix(files): match Documents filenames and mix hits into the launcher](https://github.com/ryan-stoffel/photon/pull/95)

File search on v0.2.2 still misses known Documents files and can hang on a Searching empty panel. The main launcher also does not show files unless the user enters Files mode.

Hardware screenshots (Ryan, 2026-09-14):

- Files query ember: folders from ~/Developer (ember, ember_poc, Ember Focus, ...) and two extracted .txt files; Ember_Individual_Pitch.pdf is missing
- Finder: that PDF lives at ~/Documents/School/Capstone/Individual Pitch/ (also Ember_Individual_Pitch_Bullets.pdf, Ember_Project_Proposal_Revised.pdf)
- Files query ica: stuck on Searching with an empty full-height panel

**Expected**

- ember must surface Ember_Individual_Pitch.pdf and similar files, not only folders under ~/Developer.
- Keep Spotlight/mdfind home-scope. If the metadata query misses filename tokens, add filename/basename matching (mdfind -name, and/or kMDItemDisplayName / kMDItemFSName / path predicates) so known files in ~/Documents show up.
- Do not require typing files first. If the query is not an app, Settings pane, clipboard, or calculator, automatically search files and mix those results into the main launcher.
- Never leave a stuck Searching empty panel. Time out mdfind and always leave searching status.

**Actual**

Folders match (name tokens). PDFs under Documents do not. Short queries like ica can keep FileSearchController on searching when engine.search hangs or returns nil without clearing status. Inline file hits are trailing-only, limited to three strong matches, so the main launcher hides Documents files.

**Notes**

Investigate why folders match and PDFs in Documents do not: likely kMDItemFSName == "*ember*"cd does not hit underscore-tokenized names, while folder names are exact Spotlight tokens. mdfind -name ember (and merging those paths) is the Raycast-style fallback. Rank first-token document matches high enough to appear above a screenful of ember* folders.

History: 2026-09-14 16:07 PT: Sprint PHO Sprint 1 | 2026-09-14 16:16 PT: status To Do to In Progress | 2026-09-14 16:23 PT: resolution Done; status In Progress to Done; Fix Version 0.2.3

### PHO-44: Empty launcher Down should show recents; darken top of search hairline

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.3. Labels: github, launcher.

Created 2026-09-14 16:07 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 16:26 PT.
- GitHub: [ryan-stoffel/photon#92: Empty launcher Down should show recents; darken top of search hairline](https://github.com/ryan-stoffel/photon/issues/92)
- GitHub: [ryan-stoffel/photon#96: fix(launcher): Down on empty bar shows recents; darker search hairline](https://github.com/ryan-stoffel/photon/pull/96)

The empty/default Photon bar should behave like Raycast: Down reveals recommended apps (and other recents). Separately, the hairline under the search field is too even; the top half should read slightly darker gray.

**Expected**

- Down arrow on the empty/default compact bar reveals recommended apps and other recents (frecency), without requiring Settings > Appearance > show suggestions.
- Collapse again when the query is cleared / the panel is dismissed, unless suggestions are enabled in settings.
- The hairline immediately under the search field has a slightly darker top half.

**Actual**

With suggestions off (the default), moveSelection no-ops on an empty result list, so Down does nothing. Hairline is a single Color.primary.opacity(0.08) fill.

History: 2026-09-14 16:07 PT: Sprint PHO Sprint 1 | 2026-09-14 16:17 PT: status To Do to In Progress | 2026-09-14 16:26 PT: resolution Done; status In Progress to Done; Fix Version 0.2.3

### PHO-45: Rewrite Photon in Rust + GPUI (ship as Photon.app)

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.0. Labels: github.

Created 2026-09-14 23:51 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 00:52 PT.
- relates to PHO-48
- GitHub: [ryan-stoffel/photon#100: Rewrite Photon in Rust + GPUI (ship as Photon.app)](https://github.com/ryan-stoffel/photon/issues/100)
- GitHub: [ryan-stoffel/photon#103: feat: rewrite Photon in Rust + GPUI](https://github.com/ryan-stoffel/photon/pull/103)

**Summary**

Replace the Swift/SwiftUI+AppKit app with a Rust + GPUI (Zed) rewrite. Keep the compact-bar UI, menu-bar agent, Cmd+Space launcher, and Homebrew cask. macOS 14+ only. No AI, no extension store.

**Why**

The Swift UI is still shipping clipped clipboard overlays and stuck file search. A from-scratch GPUI port lets us own layout, clipboard compact/expand, and file ranking in testable crates.

**Acceptance**

- Entire shipped app is Rust + GPUI. Swift is not the product.
- CI builds/tests on macos-latest (Linux cannot compile GPUI/AppKit).
- Homebrew cask still ships Photon.app.
- UI matches the v0.2.3 compact stills (pill bar, footer, calculator card, clipboard/files modes, settings, notes).
- Clipboard and file-search harnesses are green (see companion issues).

**Notes**

Do not invent a new visual language. Port behavior from the Swift tree.

> **Ryan Stoffel** 2026-09-14 23:58 PT: Why: the Swift UI keeps regressing clipboard overlays and file search, and I want layout and ranking in crates I can test. If the port cannot match the v0.2.3 compact stills, it does not ship.

History: 2026-09-14 23:52 PT: Sprint PHO Sprint 1 | 2026-09-15 00:07 PT: status To Do to In Progress | 2026-09-15 00:52 PT: resolution Done; status In Progress to Done; Fix Version 0.3.0

### PHO-48: Restore native macOS parity after the v0.3.0 GPUI rewrite

Bug, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.1. Labels: clipboard, github, launcher, release.

Created 2026-09-15 07:32 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 09:03 PT.
- relates to PHO-45
- GitHub: [ryan-stoffel/photon#107: Restore native macOS parity after the v0.3.0 GPUI rewrite](https://github.com/ryan-stoffel/photon/issues/107)
- GitHub: [ryan-stoffel/photon#108: fix(mac): restore native parity after v0.3.0](https://github.com/ryan-stoffel/photon/pull/108)
- GitHub: [ryan-stoffel/photon#109: fix(mac): restore native parity after v0.3.0](https://github.com/ryan-stoffel/photon/pull/109)
- Attachment: v0.3.0-dock-regression.png (image/png, 412388 bytes)

**Regression**

Photon v0.3.0 regressed native macOS launcher behavior during the Rust + GPUI rewrite. The release currently appears in the Dock, presents title/traffic-light chrome, forces light appearance, can move when interacted with, does not render application bundle icons, and lacks a verified native clipboard-hotkey session.

Verified Dock regression evidence is available from Ryan's v0.3.0 screenshot.

**Required parity gate**

Before another release, macOS runtime tests must launch the built `Photon.app` on `macos-latest` and verify:

- launcher and clipboard are borderless floating/non-activating panels
- accessory activation policy, no Dock presence, and a working menu-bar item/menu
- live system light/dark appearance
- stable panel anchor across click, typing, and expansion
- `.app` bundle icon rendering
- global `Cmd+Shift+V` clipboard session, key cycling/filtering, compact dismiss/reopen
- app/settings search, calculator, mixed/home file search (including `ember` ranking `Ember_Individual_Pitch.pdf`), recommendations, notes, settings, configurable hotkeys, drag/snap, and footer actions

If GPUI cannot pass every required check on actual macOS, restore the v0.2.3 Swift implementation as the shipping tree and release the rollback. Do not ship known-broken GPUI.

**Acceptance criteria**

- Native runtime parity harness is green on `macos-latest`
- Built UI screenshots in light and dark mode contain no title-bar traffic lights
- Shipping stack decision is documented
- Patch release is published with assets
- Homebrew cask points to the release
- Release branch is back-merged to `develop`

> **Ryan Stoffel** 2026-09-15 07:40 PT: Parity gate before any further release: packaged Photon.app on macos-latest, borderless non-activating panels, no Dock icon, live appearance, stable frame, bundle icons, and the clipboard hotkey session.

> **Ryan Stoffel** 2026-09-15 09:06 PT: Decision: GPUI failed the gate. v0.3.1 restores the v0.2.3 Swift tree and keeps the harness as a required release check.

History: 2026-09-15 07:32 PT: Sprint PHO Sprint 1 | 2026-09-15 07:40 PT: status To Do to In Progress | 2026-09-15 09:03 PT: resolution Done; status In Progress to Done; Fix Version 0.3.1

### PHO-49: v0.3.2: fix real file search and clipboard key routing

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.3.2. Labels: clipboard, files, github, launcher.

Created 2026-09-15 09:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 10:45 PT.
- GitHub: [ryan-stoffel/photon#114: v0.3.2: fix real file search and clipboard key routing](https://github.com/ryan-stoffel/photon/issues/114)
- GitHub: [ryan-stoffel/photon#115: fix(runtime): restore file search and clipboard keys](https://github.com/ryan-stoffel/photon/pull/115)

**Summary**

Photon 0.3.1 still fails on Ryan's physical Mac despite the packaged runtime checks added in #109:

- searching for a known `Ember_Individual_Pitch.pdf` in Documents returns no file results;
- Clipboard History shows rows, but physical Up/Down do not visibly change selection from either Cmd+Shift+V or the launcher command.

**Required fix**

- Make filename search work in both the mixed launcher and explicit Files mode on a real home directory, including an unindexed/lagging Spotlight fallback and bounded stale-query cancellation.
- Route clipboard navigation at the actual key NSPanel before SwiftUI's search field can consume arrows. First Down expands; subsequent Down/Up move visible selection; typing filters; Enter uses selection; dismiss/reopen resets.
- Replace the insufficient state-only runtime assertions with a required `macos-latest` end-to-end gate that launches the packaged app, sends real key events, seeds a real Documents PDF and NSPasteboard history, and asserts displayed rows/selection from the live process.
- Capture runtime screenshots as CI evidence.

**Acceptance criteria**

- `ember` displays `Ember_Individual_Pitch.pdf` in explicit Files mode and mixed main search.
- Four real clipboard entries are exercised through Cmd+Shift+V and launcher entry with Down/Down/Up visible selection changes.
- Runtime checks are bounded, fail on stale/empty UI, and are required by CI.
- Ship as v0.3.2, bump `RyanStoffel/homebrew-taps`, and back-merge `main` to `develop`.

Regression follow-up to #89, #94, #95, and #109.

History: 2026-09-15 09:50 PT: Sprint PHO Sprint 1 | 2026-09-15 09:55 PT: status To Do to In Progress | 2026-09-15 10:45 PT: resolution Done; status In Progress to Done; Fix Version 0.3.2

### PHO-53: Expand safe launcher drag surface and snap behavior

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.3. Labels: github.

Created 2026-09-15 11:32 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 12:34 PT.
- GitHub: [ryan-stoffel/photon#121: Expand safe launcher drag surface and snap behavior](https://github.com/ryan-stoffel/photon/issues/121)
- GitHub: [ryan-stoffel/photon#124: fix(launcher): expand safe drag chrome](https://github.com/ryan-stoffel/photon/pull/124)

Photon 0.3.3: allow click-and-hold drag from safe outer chrome/background points without stealing search, row, scroll, button, or footer interactions. Preserve free X/Y outside the corridor and center-snap X only inside it while retaining Y.

History: 2026-09-15 11:33 PT: Sprint PHO Sprint 1 | 2026-09-15 11:33 PT: status To Do to In Progress | 2026-09-15 12:34 PT: resolution Done; status In Progress to Done; Fix Version 0.3.3

### PHO-55: v0.3.4: Fix launcher drag guides to panel edges

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.4.7. Labels: github, launcher.

Created 2026-09-16 20:58 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:08 PT.
- blocks PHO-58
- GitHub: [ryan-stoffel/photon#130: v0.3.4: Fix launcher drag guides to panel edges](https://github.com/ryan-stoffel/photon/issues/130)

Snap guides collapsed to ±60pt corridor; restore panel left/right edge guides and center-only snap between them.

> **Ryan Stoffel** 2026-09-28 10:08 PT: Shipped in v0.3.4. Launcher drag guides mark the panel's left and right edges again, and horizontal snap uses a narrow center band. See the 0.3.4 changelog and release.

History: 2026-09-16 20:58 PT: Sprint PHO Sprint 1 | 2026-09-16 21:00 PT: status To Do to In Progress | 2026-09-28 10:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-59: v0.3.5: Vertical-only panel expansion for Files and clipboard

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.5. Labels: github, launcher.

Created 2026-09-16 22:26 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-16 23:16 PT.
- blocks PHO-62
- GitHub: [ryan-stoffel/photon#141: v0.3.5: Vertical-only panel expansion for Files and clipboard](https://github.com/ryan-stoffel/photon/issues/141)
- GitHub: [ryan-stoffel/photon#144: fix: v0.3.5 vertical expansion and real-world file search](https://github.com/ryan-stoffel/photon/pull/144)

Files mode and clipboard detail must expand downward only; keep launcher bar width fixed.

History: 2026-09-16 22:27 PT: Sprint PHO Sprint 1 | 2026-09-16 22:28 PT: status To Do to In Progress | 2026-09-16 23:16 PT: resolution Done; status In Progress to Done; Fix Version 0.3.5

### PHO-63: fix: Files and clipboard expanded UI should split side-by-side inside the compact launcher width

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.6. Labels: clipboard, files, github, launcher.

Created 2026-09-17 07:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 07:48 PT.
- GitHub: [ryan-stoffel/photon#149: fix: Files and clipboard expanded UI should split side-by-side inside the compact launcher width](https://github.com/ryan-stoffel/photon/issues/149)
- GitHub: [ryan-stoffel/photon#153: fix: compact side-by-side Files and clipboard split](https://github.com/ryan-stoffel/photon/pull/153)

**Area**

launcher / files / clipboard

**Photon version**

0.3.5

**macOS version**

26.1 (Ryan)

**Expected**

Expanded Files and Clipboard History should look like Raycast: **left list, right preview + metadata**, still inside the Appearance launcher width (620 / 740 / 860). Compact bar expands **down only**. The NSPanel must not jump wider.

**Actual**

v0.3.5 kept the compact width but stacked the list above the preview, so the panel grew very tall and no longer reads as a launcher.

Hardware references:

- `media/feedback-v035/files-stacked-ember.png` (Files `ember`)
- `media/feedback-v035/clipboard-stacked-preview.png`

**Steps to reproduce**

- Open Files, search `ember`, select a hit so preview appears.
- Open Clipboard History and press Down to expand detail.
- Observe list on top, preview below, same outer width.

**Acceptance**

- Horizontal split (list left, preview+metadata right) inside existing `panelWidth`
- No `detailWidth` / 980pt widening
- Compact bar still expands down in height only
- Packaged screenshots: Files ember side-by-side; clipboard side-by-side

History: 2026-09-17 07:21 PT: Sprint PHO Sprint 1 | 2026-09-17 07:30 PT: status To Do to In Progress | 2026-09-17 07:48 PT: resolution Done; status In Progress to Done; Fix Version 0.3.6

### PHO-64: fix: snappy launcher height animation (~120-180ms) for Files and clipboard expand

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.6. Labels: github, launcher.

Created 2026-09-17 07:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 07:48 PT.
- GitHub: [ryan-stoffel/photon#150: fix: snappy launcher height animation (~120-180ms) for Files and clipboard expand](https://github.com/ryan-stoffel/photon/issues/150)

**Area**

launcher

**Photon version**

0.3.5

**macOS version**

26.1 (Ryan)

**Expected**

Height changes when Files promotes or Clipboard History expands should stay smooth but **fast** — a snappy dropdown, about 120–180ms, ease-out. Same feel for compact bar → fullHeight.

**Actual**

`NSPanel.setFrame(display:animate:)` uses AppKit’s default window animation, which feels slow for a launcher dropdown.

**Steps to reproduce**

- Empty launcher, type `ember` until Files promotes.
- Open Clipboard History from the main list and press Enter.
- Watch the height animation.

**Acceptance**

- Expand/collapse duration is 120–180ms (ease-out)
- Width-unchanged height animation still runs (no snap-cut)
- Files promotion and clipboard Enter use the same timing

History: 2026-09-17 07:21 PT: Sprint PHO Sprint 1 | 2026-09-17 07:22 PT: status To Do to In Progress | 2026-09-17 07:48 PT: resolution Done; status In Progress to Done; Fix Version 0.3.6

### PHO-65: fix: clipboard Up/Down must highlight the selected left-list row

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.6. Labels: clipboard, github, launcher.

Created 2026-09-17 07:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 07:48 PT.
- GitHub: [ryan-stoffel/photon#151: fix: clipboard Up/Down must highlight the selected left-list row](https://github.com/ryan-stoffel/photon/issues/151)

**Area**

clipboard

**Photon version**

0.3.5

**macOS version**

26.1 (Ryan)

**Expected**

Up/Down in expanded clipboard history moves the **visible left-row highlight** with `selectionIndex`. The highlighted left title matches the item shown in the right preview.

**Actual**

The right preview already updates. The left list highlight stays on the first row (SwiftUI not redrawing list selection — same class of bug as v0.3.2 `objectWillChange`).

**Steps to reproduce**

- Seed several clipboard items.
- Open Clipboard History, expand detail.
- Press Down, Down, Up.
- Left highlight stays on row 0 while preview follows the model selection.

**Acceptance**

- Left-row highlight tracks `selectedID` / `selectedIndex`
- Packaged runtime: seed history, send Down/Down/Up, AX or screenshot OCR proves the highlighted left title matches the selected item (not stuck on the first row)

History: 2026-09-17 07:21 PT: Sprint PHO Sprint 1 | 2026-09-17 07:22 PT: status To Do to In Progress | 2026-09-17 07:48 PT: resolution Done; status In Progress to Done; Fix Version 0.3.6

### PHO-70: Files metadata overlaps the command footer

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.3.8. Labels: clipboard, files, github, launcher.

Created 2026-09-17 13:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 16:06 PT.
- GitHub: [ryan-stoffel/photon#164: Files metadata overlaps the command footer](https://github.com/ryan-stoffel/photon/issues/164)
- GitHub: [ryan-stoffel/photon#169: fix(launcher): keep Files footer, grants, drag, and snap usable](https://github.com/ryan-stoffel/photon/pull/169)
- Attachment: files-metadata-over-footer.png (image/png, 288904 bytes)

**Request**

On Photon 0.3.7, Files detail metadata (`Created` / `Modified`) paints over the bottom command row (`Open`, `Reveal in Finder`, `Space Quick Look`, `Copy Path`).

Give the Files detail pane a reserved footer-safe bottom inset so metadata never overlaps shortcuts. Apply the same treatment to clipboard Information if it can overflow.

Keep the shared 760 x 502 expanded size, instant resize, and side-by-side split.

**Acceptance criteria**

- Metadata dates remain fully above the footer; they must not collide with Open / Quick Look.
- Packaged screenshot OCR of Files recents must not see date text occupying the same vertical band as footer shortcuts.
- Clipboard Information stays inside the detail pane.
- Ember, paste, and arrow-key checks stay green.

History: 2026-09-17 13:28 PT: Sprint PHO Sprint 1 | 2026-09-17 13:36 PT: status To Do to In Progress | 2026-09-17 16:06 PT: resolution Done; status In Progress to Done; Fix Version 0.3.8

### PHO-71: Folder permission dialogs must keep the Files panel open

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.3.8. Labels: files, github, launcher.

Created 2026-09-17 13:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 16:06 PT.
- GitHub: [ryan-stoffel/photon#165: Folder permission dialogs must keep the Files panel open](https://github.com/ryan-stoffel/photon/issues/165)

**Request**

Typing a Files search still triggers TCC or open-panel prompts one folder at a time and closes Photon. Ryan has to reopen the launcher for each grant.

Required UX:

- Keep the Files UI open (panel stays visible; non-activating is fine, but it must not hide or quit).
- Queue remaining grants sequentially. macOS cannot show all TCC dialogs at once.
- After each grant, continue the search without a full relaunch.
- Persist security-scoped bookmarks.
- Prefer NSOpenPanel / security-scoped bookmarks over serial silent walks that trigger TCC from a disappearing panel.
- If a system sheet must attach, attach it to a visible Files or settings window, not by hiding the launcher.

**Acceptance criteria**

- Packaged smoke: the launcher frame exists throughout the grant flow.
- Search resumes after each grant without relaunch.
- Bookmarks persist across relaunch.
- Silent walks of ungranted Documents / Desktop / Downloads no longer fire TCC from a hidden panel.

History: 2026-09-17 13:28 PT: Sprint PHO Sprint 1 | 2026-09-17 13:30 PT: status To Do to In Progress | 2026-09-17 16:06 PT: resolution Done; status In Progress to Done; Fix Version 0.3.8

### PHO-72: Drag the launcher from the entire panel with click slop

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.3.8. Labels: github, launcher.

Created 2026-09-17 13:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 16:06 PT.
- GitHub: [ryan-stoffel/photon#166: Drag the launcher from the entire panel with click slop](https://github.com/ryan-stoffel/photon/issues/166)

**Request**

Holding the launcher hotkey and pressing mouse-down anywhere on the panel (search field, list, preview, footer chrome) should start a drag of the panel.

Do not steal clicks that need to activate rows or buttons. Raycast-like behavior: drag on chrome / empty space, and allow dragging from search-bar chrome / empty padding. For list rows, start a drag only after movement exceeds a small slop so clicks still select.

Ryan asked for the entire bar / anywhere on the UI: make the whole panel draggable with slop so clicks still work.

**Acceptance criteria**

- Packaged smoke proves mouse-down on search, list padding, preview, and footer can drag the panel.
- Row clicks still select. Footer buttons still activate.
- Ember, paste, and arrow-key checks stay green.

History: 2026-09-17 13:28 PT: Sprint PHO Sprint 1 | 2026-09-17 13:30 PT: status To Do to In Progress | 2026-09-17 16:06 PT: resolution Done; status In Progress to Done; Fix Version 0.3.8

### PHO-73: Horizontal center snap when the panel midpoint is between the edge guides

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 1. Fix version: 0.3.8. Labels: github, launcher.

Created 2026-09-17 13:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 16:06 PT.
- GitHub: [ryan-stoffel/photon#167: Horizontal center snap when the panel midpoint is between the edge guides](https://github.com/ryan-stoffel/photon/issues/167)

**Request**

Horizontal center snap is still broken. While dragging, if the panel's horizontal center is between the two dotted edge guides, snap X to screen center on release and/or live.

Guides stay at the centered panel's left and right edges.

Unit-test the snap corridor math with a wide virtual screen so the corridor is not the whole screen. Packaged smoke: drag into the corridor and assert centered X.

**Acceptance criteria**

- Snap corridor equals the span between the two edge guides (centered panel width), not a collapsed 60pt band and not the full screen width.
- Wide-screen unit tests prove inside-guide snaps and outside-guide stays free.
- Packaged smoke drags into the corridor and asserts centered X.

History: 2026-09-17 13:28 PT: Sprint PHO Sprint 1 | 2026-09-17 13:30 PT: status To Do to In Progress | 2026-09-17 16:06 PT: resolution Done; status In Progress to Done; Fix Version 0.3.8

### PHO-75: fix: launcher Down list must scroll the full catalog, not wrap

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.9. Labels: github, launcher.

Created 2026-09-17 18:25 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 19:27 PT.
- GitHub: [ryan-stoffel/photon#172: fix: launcher Down list must scroll the full catalog, not wrap](https://github.com/ryan-stoffel/photon/issues/172)
- GitHub: [ryan-stoffel/photon#175: feat(notes): match Raycast chrome and scroll launcher recs](https://github.com/ryan-stoffel/photon/pull/175)

When the default launcher is expanded (Down on the empty bar), the recommendations / apps / recent-shortcuts list should **scroll**. Pressing Down on the last *visible* row should reveal more of the full catalog, like Raycast. Up/Down must keep the selected row visible. Do not wrap to the top until the selection is truly at the last item in the full list — prefer Raycast: stop at the ends rather than cycling.

The expanded launcher must stay **760 × 502** (Regular width).

**Actual in 0.3.8**

Empty-query recommendations are truncated to one page (`suggestionCount` / 10 rows) and `moveSelection` wraps with modulo, so Down on the last visible row jumps back to the first item instead of scrolling.

**Acceptance**

- Empty-query recommendations include the full catalog (not one page)
- Down at the last visible row scrolls; it does not wrap to the first item
- Up/Down keep the selected row in view
- Expanded launcher remains 760×502
- Unit + packaged native-parity coverage

History: 2026-09-17 18:26 PT: Sprint PHO Sprint 1 | 2026-09-17 18:40 PT: status To Do to In Progress | 2026-09-17 19:27 PT: resolution Done; status In Progress to Done; Fix Version 0.3.9

### PHO-78: fix: launched apps must come to the foreground

Bug, Done (Done), priority Highest. Sprint: PHO Sprint 2. Fix version: 0.4.0. Labels: github, launcher.

Created 2026-09-20 23:17 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 00:01 PT.
- GitHub: [ryan-stoffel/photon#178: fix: launched apps must come to the foreground](https://github.com/ryan-stoffel/photon/issues/178)
- GitHub: [ryan-stoffel/photon#183: feat(launcher): foreground launch, snappy panel, native Settings](https://github.com/ryan-stoffel/photon/pull/183)

**Summary**

Opening an app from Photon currently activates it in the background. The launched or focused app must come **above everything**.

**Acceptance criteria**

- Hide Photon first, then activate the target with `NSWorkspace` launch/activate using `.activateIgnoringOtherApps` / `NSRunningApplication.activate(options: .activateIgnoringOtherApps)` (or the macOS 27 cooperative equivalent: `yieldActivation` then activate).
- Do not restore the previously frontmost app after a successful launch.
- Packaged smoke: launch a helper/target app and assert it is frontmost.

**Notes**

Photon is an accessory/`LSUIElement` agent. `hide()` currently calls `restorePreviousApplication()`, which can steal focus from the app Photon just launched.

History: 2026-09-20 23:17 PT: Sprint PHO Sprint 2 | 2026-09-20 23:25 PT: status To Do to In Progress | 2026-09-21 00:01 PT: resolution Done; status In Progress to Done; Fix Version 0.4.0

### PHO-79: feat: make launcher show, search, recs, Files, and clipboard snappy

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.0. Labels: clipboard, files, github, launcher.

Created 2026-09-20 23:17 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 00:01 PT.
- GitHub: [ryan-stoffel/photon#179: feat: make launcher show, search, recs, Files, and clipboard snappy](https://github.com/ryan-stoffel/photon/issues/179)

**Summary**

Photon should feel instant: hotkey to visible panel in one frame, typing should not hitch, recs scroll / Files / clipboard should not jank.

**Acceptance criteria**

- Defer heavy work off the first paint (do not `reloadAll` before `orderFront`).
- Cache app icons; never load Finder icons synchronously on the main thread during first draw.
- Avoid main-thread Spotlight/`mdfind` process start.
- Reduce SwiftUI invalidation while typing. Instant panel (no animation) stays.
- Cheap timing log behind `PHOTON_DEBUG=1` only. No noisy production logs.

**Notes**

Keep Files, clipboard, notes, drag, ember ranking, and recs-scroll behavior.

History: 2026-09-20 23:17 PT: Sprint PHO Sprint 2 | 2026-09-20 23:19 PT: status To Do to In Progress | 2026-09-21 00:01 PT: resolution Done; status In Progress to Done; Fix Version 0.4.0

### PHO-80: feat: Command-comma opens Settings from Photon and the launcher

Story, Done (Done), priority Highest. Sprint: PHO Sprint 2. Fix version: 0.4.0. Labels: github, launcher, settings.

Created 2026-09-20 23:17 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 00:01 PT.
- GitHub: [ryan-stoffel/photon#180: feat: Command-comma opens Settings from Photon and the launcher](https://github.com/ryan-stoffel/photon/issues/180)

**Summary**

The standard macOS Settings shortcut (⌘,) must open Photon's Settings from anywhere Photon is running, including when the launcher is key.

**Acceptance criteria**

- Wire NSApp Settings / Settings scene / menu item with ⌘,.
- The shortcut works when the launcher panel is key (nonactivating panel).
- Packaged smoke: post a CGEvent for Cmd+, and assert the settings window is visible.

**Notes**

Photon is an accessory app, so a nonactivating launcher does not get AppKit's default Settings key equivalent unless we handle it.

History: 2026-09-20 23:17 PT: Sprint PHO Sprint 2 | 2026-09-20 23:19 PT: status To Do to In Progress | 2026-09-21 00:01 PT: resolution Done; status In Progress to Done; Fix Version 0.4.0

### PHO-81: feat: native macOS 27 Settings and Liquid Glass chrome

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.0. Labels: ci, github, launcher, notes, settings.

Created 2026-09-20 23:17 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 00:01 PT.
- GitHub: [ryan-stoffel/photon#181: feat: native macOS 27 Settings and Liquid Glass chrome](https://github.com/ryan-stoffel/photon/issues/181)

**Summary**

Settings should feel like System Settings (`NavigationSplitView` + grouped `Form`) while keeping Photon's materials, accent, and typography. Launcher, Files, clipboard, Notes, and the menu bar should use Liquid Glass / macOS 27 materials when the APIs exist on the GitHub Actions runner, with a graceful fallback so macOS 14+ still builds.

**Acceptance criteria**

- Settings uses a sidebar + detail split, not a tab strip, with Photon visual language.
- Liquid Glass (`NSGlassEffectView` / equivalent) when the class exists at runtime; `NSVisualEffectView` fallback otherwise.
- Deployment target remains macOS 14+. Do not break CI if `macos-latest` is still 15/26.
- CI logs the actual runner `sw_vers` and SDK so the GitHub Actions OS is documented.

**Notes**

Do not implement Siri. Research-only Siri findings live outside this issue.

History: 2026-09-20 23:17 PT: Sprint PHO Sprint 2 | 2026-09-20 23:19 PT: status To Do to In Progress | 2026-09-21 00:01 PT: resolution Done; status In Progress to Done; Fix Version 0.4.0

### PHO-83: feat: Dock-style running indicator under open app icons

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.1. Labels: github, launcher.

Created 2026-09-21 07:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 08:16 PT.
- GitHub: [ryan-stoffel/photon#187: feat: Dock-style running indicator under open app icons](https://github.com/ryan-stoffel/photon/issues/187)
- GitHub: [ryan-stoffel/photon#190: feat(launcher): Dock-style running dots and keybind chips](https://github.com/ryan-stoffel/photon/pull/190)

**Summary**

Launcher app rows should show a small filled Dock-style dot under the icon when that application is running, matching Ryan's Raycast reference (`media/feedback-v040/dock-running-dots.png`).

**Acceptance criteria**

- Small filled dot sits under the app icon (Dock-style). Apps only (`app:` rows), not files, notes, window commands, System Settings panes, or other commands.
- Hide the dot when the app is not running. Hidden-but-running apps still show it (Dock behavior).
- Observe workspace launch/terminate so the indicator updates while the launcher is open.
- Packaged smoke: launch Calculator or TextEdit, show the launcher, assert the selected app row is running, and capture `launcher-running-dot.png`.

**Notes**

Keep files, clipboard, notes, drag, ember ranking, recs-scroll, foreground launch, ⌘,, and Settings. Do not implement Siri.

History: 2026-09-21 07:29 PT: Sprint PHO Sprint 2 | 2026-09-21 07:36 PT: status To Do to In Progress | 2026-09-21 08:16 PT: resolution Done; status In Progress to Done; Fix Version 0.4.1

### PHO-84: feat: show assigned shortcuts as trailing keybind chips

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.1. Labels: github, keybinds, launcher.

Created 2026-09-21 07:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 08:16 PT.
- GitHub: [ryan-stoffel/photon#188: feat: show assigned shortcuts as trailing keybind chips](https://github.com/ryan-stoffel/photon/issues/188)

**Summary**

When a launcher row has an assigned keybind, show it as trailing Raycast-style key chips (separate glyphs, e.g. ⌘ and /), matching Ryan's reference (`media/feedback-v040/raycast-keybind-chips.png`). Hide chips when no keybind exists.

**Acceptance criteria**

- Trailing chips on the result row, only when a shortcut is assigned (app hotkeys, window bindings, clipboard/notes hotkeys).
- Split modifiers and the key into separate chips (⌘ then /), not a single concatenated string.
- Window command rows keep a "Window" detail and move the shortcut out of the subtitle into chips.
- Packaged smoke: seed `⌘/` on Calculator or TextEdit, show the launcher, assert chip labels, and capture `launcher-keybind-chips.png`.

**Notes**

Keep files, clipboard, notes, drag, ember ranking, recs-scroll, foreground launch, ⌘,, and Settings. Do not implement Siri.

History: 2026-09-21 07:29 PT: Sprint PHO Sprint 2 | 2026-09-21 07:30 PT: status To Do to In Progress | 2026-09-21 08:16 PT: resolution Done; status In Progress to Done; Fix Version 0.4.1

### PHO-89: feat: currently open applications at the top of the launcher

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.2. Labels: github, launcher.

Created 2026-09-21 09:41 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 10:58 PT.
- GitHub: [ryan-stoffel/photon#196: feat: currently open applications at the top of the launcher](https://github.com/ryan-stoffel/photon/issues/196)

**Summary**

Currently open applications should appear at the top of the launcher list (with existing running dots), then the rest. Not files, notes, or commands unless they are apps.

**Acceptance criteria**

- Running apps sort first in the launcher list, then other apps / remaining results.
- Files, notes, and commands do not jump to the top unless they are apps.
- Existing Dock-style running dots stay on app rows.
- Packaged smoke captures launcher stills with running apps first.

**Notes**

Keep files, clipboard, notes, drag, ember ranking, recs-scroll, foreground launch, running dots, keybind chips, and ⌘,. Do not implement Siri.

History: 2026-09-21 09:41 PT: Sprint PHO Sprint 2 | 2026-09-21 09:43 PT: status To Do to In Progress | 2026-09-21 10:58 PT: resolution Done; status In Progress to Done; Fix Version 0.4.2

### PHO-92: feat: Suggestions of most-used apps at the top of the launcher

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.3. Labels: github, launcher.

Created 2026-09-21 11:33 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 12:08 PT.
- GitHub: [ryan-stoffel/photon#201: feat: Suggestions of most-used apps at the top of the launcher](https://github.com/ryan-stoffel/photon/issues/201)
- GitHub: [ryan-stoffel/photon#206: feat(launcher): most-used Suggestions, Settings focus, first-run walkthrough](https://github.com/ryan-stoffel/photon/pull/206)

**Problem**

0.4.2 pins currently open applications to the top of the launcher. That ranking is the wrong signal. Ryan wants the apps he actually opens most, not whatever happens to be running.

**Expected**

- Remove the running-apps-first sort from the launcher list.
- On the empty-query / recommendations launcher, show a Suggestions section at the top.
- Rank that section by local usage counts (how often Photon opens each app).
- Suggestions contain applications only. Files, notes, panes, and commands stay out.
- Dock-style running dots stay on open apps wherever those rows sit.

**Out of scope**

Siri. Do not drop files, clipboard, notes, drag, ember ranking, recs scroll, foreground launch, keybind chips, or Photon Settings chrome.

History: 2026-09-21 11:33 PT: Sprint PHO Sprint 2 | 2026-09-21 11:42 PT: status To Do to In Progress | 2026-09-21 12:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.3

### PHO-98: fix: Photon icon is tiny in the launcher and menus

Bug, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.4. Labels: github.

Created 2026-09-21 17:21 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 17:43 PT.
- GitHub: [ryan-stoffel/photon#211: fix: Photon icon is tiny in the launcher and menus](https://github.com/ryan-stoffel/photon/issues/211)

**Area**

launcher

**Photon version**

0.4.3

**macOS version**

26

**Expected**

The Photon row in the launcher, and Photon in menus, uses the same full rounded icon Finder shows for Photon.app.

**Actual**

The launcher row and menus draw a speck in the icon slot. Finder shows the full icon. Photo Booth and Phone fill their slots.

**Steps to reproduce**

- Install Photon 0.4.3.
- Open the launcher and search for Photon.
- Compare that row with Photo Booth or Phone, then with Photon.app in Finder.

Do not shrink the Finder icon.

History: 2026-09-21 17:21 PT: Sprint PHO Sprint 2 | 2026-09-21 17:23 PT: status To Do to In Progress | 2026-09-21 17:43 PT: resolution Done; status In Progress to Done; Fix Version 0.4.4

### PHO-106: Rank empty-launcher Suggestions by use count for apps and commands

Story, Done (Done), priority Medium. Sprint: PHO Sprint 3. Fix version: 0.4.7. Labels: github.

Created 2026-09-28 10:21 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:48 PT.
- GitHub: [ryan-stoffel/photon#232: Rank empty-launcher Suggestions by use count for apps and commands](https://github.com/ryan-stoffel/photon/issues/232)
- GitHub: [ryan-stoffel/photon#234: feat(launcher): rank Suggestions by use count](https://github.com/ryan-stoffel/photon/pull/234)

The empty launcher Suggestions list puts opened applications first, then commands in catalog order. A command he opens more often than an app still sits under every opened app.

Rank Suggestions by the local open count already stored for both applications and standing commands (Clipboard History, Search Files, Notes, window layouts). Most-used first. Recency, then title, breaks ties. Anything with no opens stays in the catalog below that prefix, which is how zero-count apps already work. File hits, individual notes, and Settings panes stay out of the prefix.

Do not pin currently running apps to the top. Running dots stay. Typed search ranking stays as it is.

History: 2026-09-28 10:22 PT: Sprint PHO Sprint 3 | 2026-09-28 10:22 PT: status To Do to In Progress | 2026-09-28 10:48 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-107: fix: typing finder hides Finder.app behind file hits

Bug, Done (Done), priority High. Sprint: PHO Sprint 3. Fix version: 0.4.7. Labels: files, github, launcher.

Created 2026-09-28 10:22 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:36 PT.
- GitHub: [ryan-stoffel/photon#233: fix: typing finder hides Finder.app behind file hits](https://github.com/ryan-stoffel/photon/issues/233)
- GitHub: [ryan-stoffel/photon#235: fix(launcher): keep Finder.app above file hits](https://github.com/ryan-stoffel/photon/pull/235)

**Area**

launcher

**Photon version**

0.4.6 (develop)

**macOS version**

26

**Expected**

Typing `finder` keeps the command list. Finder.app is the first row. Search Files and any filename hits (finder.js and similar) sit below that app. The same rule applies to other application names that also match files: the application wins, and file search follows.

**Actual**

The query promotes the launcher into Files mode as soon as filename hits arrive. The list is only files named finder. Finder.app is not shown.

**Steps to reproduce**

- Open the launcher.
- Type `finder`.
- Look at the first row.

Do not leave Finder.app behind a files-only list for this query.

History: 2026-09-28 10:22 PT: Sprint PHO Sprint 3 | 2026-09-28 10:27 PT: status To Do to In Progress | 2026-09-28 10:36 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-109: fix: launcher footer gap while results load

Bug, Done (Done), priority Medium. Sprint: PHO Sprint 3. Fix version: 0.4.8. Labels: github, launcher.

Created 2026-09-29 09:51 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-29 12:47 PT.
- GitHub: [ryan-stoffel/photon#240: fix: launcher footer gap while results load](https://github.com/ryan-stoffel/photon/issues/240)
- GitHub: [ryan-stoffel/photon#241: fix(launcher): keep the footer against the result rows](https://github.com/ryan-stoffel/photon/pull/241)

**Area**

launcher

**Photon version**

0.4.7 (`develop`)

**macOS version**

macOS 15

**Expected**

The footer stays one compact bar (Photon icon on the left, Open on the right) directly under the result rows, including while results are loading and after they arrive.

**Actual**

A short result list leaves the panel at the expanded height. Query `find` shows five rows, then a large empty region, with the footer no longer sitting against the rows.

**Steps to reproduce**

- Open the launcher.
- Type `find`.
- While results are loading, and after about five matches are on screen, look between the last row and the bottom of the panel.

History: 2026-09-29 09:52 PT: Sprint PHO Sprint 3 | 2026-09-29 09:52 PT: status To Do to In Progress | 2026-09-29 12:47 PT: resolution Done; status In Progress to Done; Fix Version 0.4.8

### PHO-111: feat: outlined Photon mark for the menu bar

Story, Done (Won't Do), priority Medium. Sprint: PHO Sprint 3. Fix version: none. Labels: github.

Created 2026-09-29 09:52 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-29 09:54 PT.
- GitHub: [ryan-stoffel/photon#243: feat: outlined Photon mark for the menu bar](https://github.com/ryan-stoffel/photon/issues/243)
- GitHub: [ryan-stoffel/photon#244: feat(menu-bar): draw an outlined Photon mark](https://github.com/ryan-stoffel/photon/pull/244)

The menu bar status item uses the filled SF Symbol `sun.max.fill`. Replace it with an outlined template image of the Photon mark.

**Summary**

Stroke the Photon mark (the rounded tile and its horizontal light streak) instead of a filled color icon. Render that image as an `NSImage` template so light and dark menu bars tint it, and keep the stroke sharp at menu-bar size.

**Acceptance criteria**

- The status item image is an outline of the Photon mark, not `sun.max.fill` and not the filled color app icon
- The release image uses template rendering so it reads in light and dark menu bars
- The mark stays sharp at menu-bar size
- `Resources/Photon.icns` and the Dock / Finder icon are unchanged
- The Photon-Dev menu-bar image still gets the yellow "dev" tag

**Notes**

The Dock and Finder icon stay `Resources/Photon.icns`.

> **Ryan Stoffel** 2026-09-29 09:54 PT: Not doing the outlined menu bar icon. The current menu bar icon stays.

History: 2026-09-29 09:53 PT: Sprint PHO Sprint 3 | 2026-09-29 09:52 PT: status To Do to In Progress | 2026-09-29 09:54 PT: resolution Won't Do; status In Progress to Done

### PHO-112: feat(launcher): Raycast parity pass

Story, In Progress, priority Medium. Sprint: PHO Sprint 3. Fix version: none. Labels: github, launcher.

Created 2026-10-03 19:42 PT by Ryan Stoffel, assigned to Ryan Stoffel.
- GitHub: [ryan-stoffel/photon#248: feat(launcher): Raycast parity pass](https://github.com/ryan-stoffel/photon/issues/248)
- GitHub: [ryan-stoffel/photon#249: feat(launcher): Raycast parity pass](https://github.com/ryan-stoffel/photon/pull/249)

**Area**

launcher

**Summary**

Photon should behave like a minimal Raycast: no AI and no extensions, with the core launcher interactions and built-in features working the way Raycast's do. An audit of v0.4.8 found these problems:

- Root search fills with loosely matching System Settings panes. Typing `saf` never lists Safari, and `settings` buries the System Settings app.
- Root queries switch into Files mode when Spotlight returns hits.
- There is no ⌘K action panel and no action bar. Esc does not clear typed text first, and the panel does not open on the display with the mouse.
- The calculator crashes on `2^64` and gives wrong answers for negative operands (`2*-3` = -3).
- Clipboard History opens collapsed, and an index that fails to load wipes the history.
- Notes, keybinds, and Settings have their own defects: the format bar blocks editing, Hyper can stick, and Settings has no Edit menu.

**Acceptance criteria**

- Root search ranks exact titles first and shows real System Settings pane names. File hits stay as rows and never switch modes.
- ⌘K opens an action panel for root, Files, and Clipboard. The footer shows the primary action and Actions ⌘K.
- Esc closes the panel first, then clears the text, then leaves the mode, then hides the launcher.
- The calculator handles unary minus, large results, and units.
- Clipboard opens with the list and detail visible, and corrupt history is quarantined instead of lost.
- Notes, keybinds, and Settings defects from the audit are fixed.
- CI is green.

> **Ryan Stoffel** 2026-10-03 20:12 PT: Scope stays the same: no AI and no extensions. This is about root search ranking, Esc order, an action panel, the calculator, and clipboard reliability.

History: 2026-10-03 19:43 PT: Sprint PHO Sprint 3 | 2026-10-03 20:09 PT: status To Do to In Progress

## Epic PHO-2: Clipboard history (7 issues)

### PHO-2: Clipboard history

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:42 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:13 PT.

Clipboard capture, history UI, and paste-back.

History: 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-28 10:13 PT: resolution Done; status In Progress to Done

### PHO-10: Clipboard history

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: clipboard, github.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 20:42 PT.
- relates to PHO-19
- GitHub: [ryan-stoffel/photon#3: Clipboard history](https://github.com/ryan-stoffel/photon/issues/3)
- GitHub: [ryan-stoffel/photon#16: feat(clipboard): add clipboard history](https://github.com/ryan-stoffel/photon/pull/16)

**Summary**

Phase 2. Searchable clipboard history for text, links, images, and files, with pin, paste/copy-back, retention, and excluded apps (password managers).

**Acceptance criteria**

- Implemented in `Sources/PhotonClipboard/` as a `CommandProvider`
- Registered from `AppRuntime` only
- Settings > Clipboard is wired to real controls
- Password managers can be excluded

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 20:40 PT: status To Do to In Progress | 2026-09-13 20:42 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-29: Restyle clipboard mode to match compact launcher

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.0. Labels: github.

Created 2026-09-14 07:49 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 08:08 PT.
- GitHub: [ryan-stoffel/photon#49: Restyle clipboard mode to match compact launcher](https://github.com/ryan-stoffel/photon/issues/49)
- GitHub: [ryan-stoffel/photon#53: Restyle clipboard mode to match compact launcher](https://github.com/ryan-stoffel/photon/pull/53)

**Problem**

Clipboard mode opens a tall panel with a large empty region below the search field. The blue \"Clipboard\" pill sits beside the placeholder and competes for attention, and the layout does not match the redesigned compact launcher (search field + growing result list).

**Expected**

- Compact panel height when there is nothing to list (search field + footer only), growing row-by-row as history items appear—same sizing rules as the main launcher.
- Same search field treatment as the default launcher (no mode pill in the search row).
- Subtle clipboard mode indicator (footer accessory, not a prominent pill).
- Result rows aligned with launcher rows: type icon, snippet, relative time as secondary text.
- Preserve existing actions: pin, delete, paste/copy, clear-all confirmation, keyboard shortcuts.
- Empty states: one short line of copy, not a tall empty pane.

**Context**

Feedback from v0.1.1 visual QA (2026-09-14).

History: 2026-09-14 07:50 PT: Sprint PHO Sprint 1 | 2026-09-14 07:51 PT: status To Do to In Progress | 2026-09-14 08:08 PT: resolution Done; status In Progress to Done; Fix Version 0.2.0

### PHO-34: Cmd+Shift+V clipboard should use compact launcher bar

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.1. Labels: github.

Created 2026-09-14 11:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 12:12 PT.
- GitHub: [ryan-stoffel/photon#64: Cmd+Shift+V clipboard should use compact launcher bar](https://github.com/ryan-stoffel/photon/issues/64)
- GitHub: [ryan-stoffel/photon#68: fix(clipboard): compact hotkey panel (GH-64)](https://github.com/ryan-stoffel/photon/pull/68)

v0.2.1 bug fix

History: 2026-09-14 11:20 PT: Sprint PHO Sprint 1 | 2026-09-14 11:27 PT: status To Do to In Progress | 2026-09-14 12:12 PT: resolution Done; status In Progress to Done; Fix Version 0.2.1

### PHO-46: Clipboard compact bar: Down expands, dismiss restores compact (no overlay)

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.0. Labels: github.

Created 2026-09-14 23:51 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 00:52 PT.
- GitHub: [ryan-stoffel/photon#101: Clipboard compact bar: Down expands, dismiss restores compact (no overlay)](https://github.com/ryan-stoffel/photon/issues/101)

**Summary**

Cmd+Shift+V must open a compact bar. Down expands history. Up/Down cycle items with and without a query. Dismiss and reopen (and opening clipboard from the main launcher) must restore the compact bar.

**Bug (0.2.x)**

Huge dim overlay, clipped chrome. See media/feedback-2026-09-14-v022/.

**Acceptance**

- Compact height = 89pt (56+1+32) until Down expands.
- Typing filters; arrows still move selection.
- Persist history (text/links/images/files), pin, paste/copy, excluded apps.
- Headless tests: Down expands, Up/Down move, dismiss resets compact.
- Screenshot scenarios vs compact-bar stills (no overlay > 420px).
- Do not merge until the harness is green.

History: 2026-09-14 23:52 PT: Sprint PHO Sprint 1 | 2026-09-14 23:53 PT: status To Do to In Progress | 2026-09-15 00:52 PT: resolution Done; status In Progress to Done; Fix Version 0.3.0

### PHO-50: Fix trusted clipboard paste and add expanded detail view

Bug, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.3. Labels: github.

Created 2026-09-15 11:32 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 12:41 PT.
- GitHub: [ryan-stoffel/photon#118: Fix trusted clipboard paste and add expanded detail view](https://github.com/ryan-stoffel/photon/issues/118)
- GitHub: [ryan-stoffel/photon#122: fix(clipboard): restore trusted paste and add detail view](https://github.com/ryan-stoffel/photon/pull/122)

Photon 0.3.3: restore the previously focused target before injecting Cmd+V, distinguish AX trust from event-delivery failure, and add Raycast-style text/image detail with metadata. Acceptance requires packaged-app sentinel paste and rendered detail checks on macos-latest.

History: 2026-09-15 11:33 PT: Sprint PHO Sprint 1 | 2026-09-15 11:33 PT: status To Do to In Progress | 2026-09-15 12:41 PT: resolution Done; status In Progress to Done; Fix Version 0.3.3

### PHO-56: v0.3.4: Fix clipboard Enter paste on real Mac

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.4.7. Labels: clipboard, github.

Created 2026-09-16 20:58 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:08 PT.
- blocks PHO-58
- GitHub: [ryan-stoffel/photon#131: v0.3.4: Fix clipboard Enter paste on real Mac](https://github.com/ryan-stoffel/photon/issues/131)

Paste still fails with Accessibility granted; restore target activation and delivery-time trust checks.

> **Ryan Stoffel** 2026-09-28 10:08 PT: Shipped in v0.3.4. Clipboard paste hides Photon, reactivates the prior app, and checks Accessibility at delivery time so Enter reaches the focused field. See the 0.3.4 changelog and release.

History: 2026-09-16 20:58 PT: Sprint PHO Sprint 1 | 2026-09-16 21:00 PT: status To Do to In Progress | 2026-09-28 10:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-68: Unify expanded launcher, Files, and clipboard dimensions

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.7. Labels: github.

Created 2026-09-17 11:44 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 12:03 PT.
- GitHub: [ryan-stoffel/photon#159: Unify expanded launcher, Files, and clipboard dimensions](https://github.com/ryan-stoffel/photon/issues/159)
- GitHub: [ryan-stoffel/photon#161: fix(layout): unify expanded panel dimensions](https://github.com/ryan-stoffel/photon/pull/161)

**Request**

Implement Ryan's v0.3.7 layout update:

- Make the default compact launcher bar modestly wider.
- Pressing Down on the empty default bar expands vertically only, with no horizontal resize.
- Files, expanded Clipboard History, and launcher recommendations must have exactly the same outer width and height.
- Keep the Files and clipboard list/detail split inside that shared panel size.
- Keep Files and clipboard compact until they have results or are expanded.
- Remove the resize animation; panel size changes must be instant.

**Acceptance criteria**

- Unit/layout tests compare Files, clipboard expanded, and launcher recommendations panel width and height for exact equality.
- Packaged native-parity checks measure and compare all three frames, including unchanged width from compact to expanded launcher.
- Screenshot artifacts include launcher recommendations, Files, and clipboard at matching dimensions.
- Existing smoke, paste, guides, and `ember` scenarios remain green.

History: 2026-09-17 11:45 PT: Sprint PHO Sprint 1 | 2026-09-17 11:48 PT: status To Do to In Progress | 2026-09-17 12:03 PT: resolution Done; status In Progress to Done; Fix Version 0.3.7

## Epic PHO-3: Notes (4 issues)

### PHO-3: Notes

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:43 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 19:32 PT.

Floating markdown notes.

History: 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-17 19:32 PT: resolution Done; status In Progress to Done

### PHO-11: Simple notes

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: github, notes.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 21:03 PT.
- GitHub: [ryan-stoffel/photon#4: Simple notes](https://github.com/ryan-stoffel/photon/issues/4)
- GitHub: [ryan-stoffel/photon#20: feat(notes): add floating markdown notes with launcher integration](https://github.com/ryan-stoffel/photon/pull/20)

**Summary**

Phase 2. Raycast Notes-like floating notes: lightweight markdown, multiple notes, persistent, searchable from the launcher.

**Acceptance criteria**

- Implemented in `Sources/PhotonNotes/` as a `CommandProvider`
- Notes persist locally
- Searchable from the launcher
- Settings > Notes is wired to real controls

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 21:00 PT: status To Do to In Progress | 2026-09-13 21:03 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-23: feat: notes window with a native sidebar and standard toolbar

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: github, notes.

Created 2026-09-13 22:49 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 23:50 PT.
- GitHub: [ryan-stoffel/photon#32: feat: notes window with a native sidebar and standard toolbar](https://github.com/ryan-stoffel/photon/issues/32)
- GitHub: [ryan-stoffel/photon#36: feat(notes): native sidebar and unified toolbar for the notes window](https://github.com/ryan-stoffel/photon/pull/36)

**Area**

notes

**Summary**

Ryan tested v0.1.0: notes work, but the window's chrome does not feel like macOS. His feedback:

> I don't like the icons. For the icon on the left to search for other notes, instead make that a sidebar on the left and use a native sidebar icon that indicates that; move the new note icon into an action as part of that sidebar. Make the icons feel more native, doesn't really fit the macOS vibe.

Replace the `list.bullet` popover switcher with a real collapsible sidebar and rebuild the toolbar with the standard toolbar system so the window reads like Apple Notes.

**Acceptance criteria**

- The note list is a collapsible sidebar (`NSSplitViewController` sidebar item, `.sidebar` material): title, one-line snippet, relative date; newest first; the current note is selected. Sidebar collapsed state and width are remembered across launches.
- `Cmd+P` expands the sidebar if needed and focuses the list (arrow keys switch notes, Return or Escape go back to the editor). No separate popover.
- Toolbar uses the standard toolbar system with the `.unified` style and untinted SF Symbols at the default toolbar size: the standard sidebar toggle (`sidebar.left`, `toggleSidebar:`), "New Note" (`square.and.pencil`) next to it in the sidebar's toolbar area, a tracking separator, and an `ellipsis.circle` menu with Float on Top, Reveal in Finder, and Delete Note.
- Editor is closer to Apple Notes: the first line is styled as a title, body in the system font at 13-14 pt, 16-20 pt insets, comfortable line spacing. Storage stays plain markdown and the existing `MarkdownStyler` engine is kept.
- The window title is the current note's title; the window remains a floating-capable non-activating panel.
- Unit tests cover the pure parts (sidebar row model, ordering, date text, title span); `CHANGELOG.md` (Unreleased) and the PhotonNotes section of `docs/architecture.md` are updated.

**Notes**

- Reference: Apple Notes on macOS 14/15 (sidebar toggle + compose button over the list, title over the editor, `...` menu at the trailing edge).
- The implementing worker runs on Linux; CI is the compiler. A Mac-based visual QA pass follows the PR.

History: 2026-09-13 22:49 PT: Sprint PHO Sprint 1 | 2026-09-13 23:09 PT: status To Do to In Progress | 2026-09-13 23:50 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-25: bug: notes sidebar is not full height and the toolbar title precedes the sidebar controls

Bug, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: github, notes.

Created 2026-09-13 23:56 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 00:07 PT.
- GitHub: [ryan-stoffel/photon#37: bug: notes sidebar is not full height and the toolbar title precedes the sidebar controls](https://github.com/ryan-stoffel/photon/issues/37)
- GitHub: [ryan-stoffel/photon#38: fix(notes): full-height sidebar and tracking separator need fullSizeContentView](https://github.com/ryan-stoffel/photon/pull/38)

**Area**

notes

**What happens**

On `develop` after #36, the notes window's sidebar stops below the toolbar instead of running the full height of the window, the window title sits at the leading edge before the sidebar toggle and "New Note" buttons, and the toolbar's tracking separator does not line up with the split divider (CI capture: `docs/screenshots/notes-light.png` at `590835c`).

**Why**

`NotesWindow` dropped `.fullSizeContentView` from the panel's style mask. AppKit only lays out the full-height sidebar and the `.sidebarTrackingSeparator` section when the window uses `.fullSizeContentView` (WWDC20 "Adopt the new look of macOS"); without it the separator degrades to a plain separator and the `.unified` title stays leading.

**Fix**

Add `.fullSizeContentView` back (v0.1.0 had it). The editor's scroll view and the SwiftUI sidebar list inset themselves below the toolbar automatically. Re-capture `docs/screenshots/notes-*.png`.

**Acceptance criteria**

- Sidebar material reaches the top of the window; toolbar reads `[toggle] [New Note] | <note title> ... [more]`.
- Tracking separator sits on the split divider and follows it when the sidebar is resized.
- Editor text and sidebar rows start below the toolbar (no content hidden under it).

History: 2026-09-13 23:56 PT: Sprint PHO Sprint 1 | 2026-09-13 23:56 PT: status To Do to In Progress | 2026-09-14 00:07 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-76: feat: revamp Notes to match Raycast Notes chrome

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.9. Labels: github, notes.

Created 2026-09-17 18:25 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 19:27 PT.
- GitHub: [ryan-stoffel/photon#173: feat: revamp Notes to match Raycast Notes chrome](https://github.com/ryan-stoffel/photon/issues/173)

Revamp Photon Notes to match Raycast Notes as closely as SwiftUI/AppKit allow (no Raycast assets). Ryan's 0.3.8 reference shots are authoritative:

- notes editor vs Raycast (vibrancy, title, traffic lights, character count)
- notes switcher overlay (search, pin, delete, current indicator)
- actions command palette (⌘K)
- formatting toolbar

**Acceptance**

- Semi-transparent / vibrancy background
- Same overall chrome: title, traffic lights or Photon-appropriate window, editor, character count
- Notes switcher overlay (search notes, pin, delete, current indicator)
- Actions command palette (⌘K) with New Note, Duplicate, Browse Notes, Find in Note, Copy, Deeplink if feasible, Export, list item move, Format
- Formatting toolbar: headings, bold, italic, strikethrough, underline, code, link, lists
- Fixed horizontal width ~640–700pt (Regular launcher is 760pt). Height can grow; width stays constant
- SF Symbols equivalents, not copied Raycast assets
- Files/clipboard/drag/paste/ember keep working; do not regress 760×502 launcher expanded size
- Unit test: notes window width is constant
- Screenshots: notes editor, switcher, actions, launcher recs scrolled

Keep markdown files, autosave, launcher `notes` / `n` search, and existing hotkeys. Deeplink can be `photon://note/<id>` if we register a URL scheme.

History: 2026-09-17 18:26 PT: Sprint PHO Sprint 1 | 2026-09-17 18:27 PT: status To Do to In Progress | 2026-09-17 19:27 PT: resolution Done; status In Progress to Done; Fix Version 0.3.9

## Epic PHO-4: File search (9 issues)

### PHO-4: File search

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:43 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-16 23:21 PT.

Spotlight file search, Files mode, folder access.

History: 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-16 23:21 PT: resolution Done; status In Progress to Done

### PHO-12: File search

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: files, github.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 20:56 PT.
- relates to PHO-19
- GitHub: [ryan-stoffel/photon#5: File search](https://github.com/ryan-stoffel/photon/issues/5)
- GitHub: [ryan-stoffel/photon#18: feat(files): Spotlight file search with Quick Look](https://github.com/ryan-stoffel/photon/pull/18)

**Summary**

Phase 2. Whole-Mac search via Spotlight (`NSMetadataQuery`), Quick Look, open / reveal in Finder / copy path.

**Acceptance criteria**

- Implemented in `Sources/PhotonFiles/` as a `CommandProvider`
- Backed by `NSMetadataQuery`, not a custom crawler
- Open, reveal in Finder, and copy path work
- Settings > Files is wired to real controls

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 20:53 PT: status To Do to In Progress | 2026-09-13 20:56 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-28: File search: default to home folder, fuzzy matching, launcher-style Files UI

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.0. Labels: github.

Created 2026-09-14 07:48 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 08:03 PT.
- GitHub: [ryan-stoffel/photon#47: File search: default to home folder, fuzzy matching, launcher-style Files UI](https://github.com/ryan-stoffel/photon/issues/47)
- GitHub: [ryan-stoffel/photon#52: File search: home scope, fuzzy matching, launcher-style Files UI](https://github.com/ryan-stoffel/photon/pull/52)

**Problem**

File search defaults to the whole Mac, so Spotlight surfaces irrelevant system paths (for example under `/System/Volumes/Data/...`). Matching is substring-based on the file stem and does not tolerate underscores vs spaces the way the app launcher does. Files mode rows and the empty state do not match the redesigned launcher (row height, icon+name with path as secondary text on one line).

**Proposal**

- Default search scope: **Home folder** (`~/`), with Settings still offering **This Mac** plus extra/excluded folders.
- **Fuzzy matching** on the last path component and on the path relative to home (case-insensitive; underscore/space/punctuation tolerant; subsequence scoring like `FuzzyMatcher`).
- Example: `~/Documents/School/Capstone/Individual Pitch/Ember_Individual_Pitch.pdf` should match queries like `ember_individual`.
- **Files mode UI**: match launcher rows (40pt, rounded selection, icon + name + truncated `~/...` subtitle on the same baseline). Native empty state copy (home-scoped, not “anywhere on this Mac”).
- Unit tests for fuzzy matching and home-relative path display/ranking.

**Acceptance**

- Default scope is Home for new installs; existing settings unchanged.
- Fuzzy + path tests pass in CI.
- Changelog updated under Unreleased.

History: 2026-09-14 07:49 PT: Sprint PHO Sprint 1 | 2026-09-14 07:50 PT: status To Do to In Progress | 2026-09-14 08:03 PT: resolution Done; status In Progress to Done; Fix Version 0.2.0

### PHO-33: File search defaults to home scope and filters system paths

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.2.1. Labels: github.

Created 2026-09-14 11:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 11:44 PT.
- GitHub: [ryan-stoffel/photon#63: File search defaults to home scope and filters system paths](https://github.com/ryan-stoffel/photon/issues/63)
- GitHub: [ryan-stoffel/photon#67: fix(files): home-only Spotlight scope (GH-63)](https://github.com/ryan-stoffel/photon/pull/67)

v0.2.1 bug fix

History: 2026-09-14 11:20 PT: Sprint PHO Sprint 1 | 2026-09-14 11:26 PT: status To Do to In Progress | 2026-09-14 11:44 PT: resolution Done; status In Progress to Done; Fix Version 0.2.1

### PHO-39: File search returns nothing; use Spotlight mdfind in home

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.2.2. Labels: files, github.

Created 2026-09-14 14:16 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-14 14:38 PT.
- GitHub: [ryan-stoffel/photon#79: File search returns nothing; use Spotlight mdfind in home](https://github.com/ryan-stoffel/photon/issues/79)
- GitHub: [ryan-stoffel/photon#83: fix(files): search home with Spotlight mdfind](https://github.com/ryan-stoffel/photon/pull/83)

**Summary**

Files mode returns no results. Search should follow Raycast: Spotlight via `mdfind`, scoped to the home folder, not a custom filesystem walk and not `NSMetadataQuery` in an agent app.

v0.2.1. Example: `ember_individual` should find a file under `~/Documents` (e.g. `Ember_Individual_Pitch.pdf`).

**Expected**

- Query Spotlight with `/usr/bin/mdfind -onlyin $HOME` (plus extra folders). Split punctuation so `ember_individual` is `ember` AND `individual`.
- Home-only: drop `/System`, `/Library` (except `~/Library`), `/private`, `/usr`, `/bin`. Resolve `/System/Volumes/Data` firmlinks before filtering.
- Empty Files panel stays compact until the user types. Footer shows Files.
- Rank hits with the existing fuzzy matcher.

**Actual**

`NSMetadataQuery` in an `LSUIElement` app often yields nothing. The Spotlight string keeps the underscore as one token (`*ember_individual*`), which does not match `Ember_Individual_Pitch.pdf`. Home-scope filtering treats firmlink paths under `/System/Volumes/Data/Users/...` as blocked `/System` paths.

History: 2026-09-14 14:16 PT: Sprint PHO Sprint 1 | 2026-09-14 14:24 PT: status To Do to In Progress | 2026-09-14 14:38 PT: resolution Done; status In Progress to Done; Fix Version 0.2.2

### PHO-47: File search: ember ranks Ember_Individual_Pitch.pdf; mix into launcher; no stuck Searching

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.0. Labels: github.

Created 2026-09-14 23:51 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 00:52 PT.
- GitHub: [ryan-stoffel/photon#102: File search: ember ranks Ember_Individual_Pitch.pdf; mix into launcher; no stuck Searching](https://github.com/ryan-stoffel/photon/issues/102)

**Summary**

Home-only Spotlight/mdfind plus filename/basename matching so \`ember\` returns Ember_Individual_Pitch.pdf under ~/Documents, not only folders in ~/Developer.

**Bug (0.2.x)**

Folders-only ember, stuck Searching…. See media/feedback-2026-09-14-v022/.

**Acceptance**

- Do not require typing \"files\". If the query is not an app, Settings pane, clipboard, or calculator, search files and show them in the main launcher.
- No stuck Searching…. Empty files panel is compact until there are results.
- Headless/unit test with a fixture home tree that asserts ember ranks Ember_Individual_Pitch.pdf.
- If CI Spotlight is empty, the fixture/mdfind -onlyin test dir must still pass without a full-disk index.
- Do not merge a files PR until the harness is green.

History: 2026-09-14 23:52 PT: Sprint PHO Sprint 1 | 2026-09-14 23:53 PT: status To Do to In Progress | 2026-09-15 00:52 PT: resolution Done; status In Progress to Done; Fix Version 0.3.0

### PHO-51: Replace protected-folder prompt cascade with guided file access

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.3. Labels: github.

Created 2026-09-15 11:32 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 13:59 PT.
- GitHub: [ryan-stoffel/photon#119: Replace protected-folder prompt cascade with guided file access](https://github.com/ryan-stoffel/photon/issues/119)
- GitHub: [ryan-stoffel/photon#123: fix(files): add guided persistent folder access](https://github.com/ryan-stoffel/photon/pull/123)

Photon 0.3.3: stop launcher-time protected-folder walks. Add a normal one-time folder grant flow using NSOpenPanel and persistent security-scoped bookmarks, with cancel/error/resume handling and packaged runtime integration checks.

History: 2026-09-15 11:33 PT: Sprint PHO Sprint 1 | 2026-09-15 11:33 PT: status To Do to In Progress | 2026-09-15 13:59 PT: resolution Done; status In Progress to Done; Fix Version 0.3.3

### PHO-52: Add Files recents and persistent preview metadata

Story, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.3. Labels: github.

Created 2026-09-15 11:32 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-15 11:54 PT.
- GitHub: [ryan-stoffel/photon#120: Add Files recents and persistent preview metadata](https://github.com/ryan-stoffel/photon/issues/120)
- GitHub: [ryan-stoffel/photon#125: feat(files): add recents and persistent detail preview](https://github.com/ryan-stoffel/photon/pull/125)

Photon 0.3.3: show Recent Files and Quick Look-quality preview/metadata in empty Files mode, retain split detail for query results, and update previews with keyboard selection. Gate known PDF/image rendering on macos-latest.

History: 2026-09-15 11:33 PT: Sprint PHO Sprint 1 | 2026-09-15 11:37 PT: status To Do to In Progress | 2026-09-15 11:54 PT: resolution Done; status In Progress to Done; Fix Version 0.3.3

### PHO-54: v0.3.4: Restore PDF file search and Files split UI from main bar

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.4. Labels: files, github.

Created 2026-09-16 20:58 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-16 21:24 PT.
- blocks PHO-58
- GitHub: [ryan-stoffel/photon#129: v0.3.4: Restore PDF file search and Files split UI from main bar](https://github.com/ryan-stoffel/photon/issues/129)
- GitHub: [ryan-stoffel/photon#133: fix: v0.3.4 regressions (files, drag, clipboard, parity)](https://github.com/ryan-stoffel/photon/pull/133)

Regressions from v0.3.3: PDFs missing in search; main-bar file queries must open full Files session (recents, split preview, footer).

History: 2026-09-16 20:58 PT: Sprint PHO Sprint 1 | 2026-09-16 20:59 PT: status To Do to In Progress | 2026-09-16 21:24 PT: resolution Done; status In Progress to Done; Fix Version 0.3.4

### PHO-60: v0.3.5: Fix real-world file search (ember PDF under Documents)

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.5. Labels: files, github.

Created 2026-09-16 22:26 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-16 23:16 PT.
- blocks PHO-62
- GitHub: [ryan-stoffel/photon#142: v0.3.5: Fix real-world file search (ember PDF under Documents)](https://github.com/ryan-stoffel/photon/issues/142)

Fallback must walk granted and standard user folders; fix promotion/search races.

History: 2026-09-16 22:27 PT: Sprint PHO Sprint 1 | 2026-09-16 22:28 PT: status To Do to In Progress | 2026-09-16 23:16 PT: resolution Done; status In Progress to Done; Fix Version 0.3.5

## Epic PHO-5: Keybinds and window management (3 issues)

### PHO-5: Keybinds and window management

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:44 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 11:03 PT.

Hyper key, app shortcuts, window commands.

History: 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-21 11:03 PT: resolution Done; status In Progress to Done

### PHO-13: Keybinds and window management

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: github, keybinds.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 21:09 PT.
- GitHub: [ryan-stoffel/photon#6: Keybinds and window management](https://github.com/ryan-stoffel/photon/issues/6)
- GitHub: [ryan-stoffel/photon#19: feat(keybinds): Hyper key, app hotkeys, and window management](https://github.com/ryan-stoffel/photon/pull/19)

**Summary**

Phase 2. Configurable Hyper key (default Caps Lock → Ctrl+Opt+Shift+Cmd), user-defined hotkeys to launch or focus apps, and window management (halves, thirds, maximize, center, next display) via the Accessibility API.

**Acceptance criteria**

- Implemented in `Sources/PhotonKeybinds/`
- Hyper key can be enabled/disabled
- App launch/focus hotkeys work
- Window commands work on the frontmost window
- Settings > Keybinds is wired to real controls
- Accessibility permission is requested with a clear explanation

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 20:58 PT: status To Do to In Progress | 2026-09-13 21:09 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-87: fix: Caps Lock Hyper key must not toggle Caps Lock

Bug, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.2. Labels: github, keybinds.

Created 2026-09-21 09:41 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 10:58 PT.
- GitHub: [ryan-stoffel/photon#194: fix: Caps Lock Hyper key must not toggle Caps Lock](https://github.com/ryan-stoffel/photon/issues/194)

**Summary**

When Caps Lock is the Hyper key, Caps Lock state must not turn on. Typing with Hyper currently leaves CAPS on.

**Acceptance criteria**

- Caps Lock as Hyper never toggles the Caps Lock LED/state on.
- Hyper chords still fire (app hotkeys, window commands).
- Packaged smoke (or unit coverage) asserts Caps Lock state stays off when Hyper is bound to Caps Lock.

**Notes**

Keep files, clipboard, notes, drag, ember ranking, recs-scroll, foreground launch, running dots, keybind chips, and ⌘,. Do not implement Siri.

History: 2026-09-21 09:41 PT: Sprint PHO Sprint 2 | 2026-09-21 09:43 PT: status To Do to In Progress | 2026-09-21 10:58 PT: resolution Done; status In Progress to Done; Fix Version 0.4.2

### PHO-88: feat: list installed apps then assign app shortcuts

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.2. Labels: github, keybinds, settings.

Created 2026-09-21 09:41 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 10:58 PT.
- GitHub: [ryan-stoffel/photon#195: feat: list installed apps then assign app shortcuts](https://github.com/ryan-stoffel/photon/issues/195)

**Summary**

Replace the Add Application… picker plus warning-triangle row in App hotkeys. Show installed/current apps in a list; each row can get a shortcut. Keep a way to add something missing if needed.

**Acceptance criteria**

- App hotkeys lists installed/current apps (not only previously picked apps).
- Each row can assign a shortcut. Remove the warning-triangle row as the primary UX.
- Keep a way to add an app that is missing from the list.
- Packaged smoke captures `app-hotkeys` stills of the list (not the old picker-only row).

**Notes**

Keep files, clipboard, notes, drag, ember ranking, recs-scroll, foreground launch, running dots, keybind chips, and ⌘,. Do not implement Siri.

History: 2026-09-21 09:41 PT: Sprint PHO Sprint 2 | 2026-09-21 09:43 PT: status To Do to In Progress | 2026-09-21 10:58 PT: resolution Done; status In Progress to Done; Fix Version 0.4.2

## Epic PHO-6: Settings (4 issues)

### PHO-6: Settings

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:44 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-29 12:44 PT.

Settings window and preferences.

History: 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-29 12:44 PT: resolution Done; status In Progress to Done

### PHO-14: Settings window

Story, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: github, settings.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 19:56 PT.
- GitHub: [ryan-stoffel/photon#7: Settings window](https://github.com/ryan-stoffel/photon/issues/7)

**Summary**

Native settings window with tabs: General, Clipboard, Notes, Files, Keybinds, About. Phase 1 ships General (hotkey + launch at login) and About. Other tabs are placeholders bound to `SettingsStore`.

**Acceptance criteria**

- Settings window opens from the menu bar extra
- General: hotkey recorder and launch-at-login via `SMAppService`
- About: version from `VERSION` / `PhotonVersion`, bundle id, license
- Placeholder tabs read and write `SettingsStore` keys

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 19:52 PT: status To Do to In Progress | 2026-09-13 19:56 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-86: feat: restyle Settings to match the Photon launcher

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.2. Labels: github, settings.

Created 2026-09-21 09:41 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 10:58 PT.
- GitHub: [ryan-stoffel/photon#193: feat: restyle Settings to match the Photon launcher](https://github.com/ryan-stoffel/photon/issues/193)
- GitHub: [ryan-stoffel/photon#198: feat(settings): Photon chrome, Caps Lock Hyper, app list, running apps first](https://github.com/ryan-stoffel/photon/pull/198)

**Summary**

Ryan rejected the current Settings window (System Settings clone). Restyle it to match the launcher panel: materials, corner radius, type, and chrome. Keep ⌘, to open Settings. Native macOS 14+; Liquid Glass only if it already matches the panel.

**Acceptance criteria**

- Settings uses Photon's launcher materials, radius, type, and chrome — not a generic System Settings clone.
- ⌘, still opens Settings.
- Native macOS 14+. Liquid Glass only if it already matches the launcher panel.
- Packaged smoke still asserts Settings is visible after ⌘, and captures a Photon-styled `settings-general.png` (and related Settings stills).

**Notes**

Keep files, clipboard, notes, drag, ember ranking, recs-scroll, foreground launch, running dots, keybind chips, and ⌘,. Do not implement Siri.

History: 2026-09-21 09:41 PT: Sprint PHO Sprint 2 | 2026-09-21 09:57 PT: status To Do to In Progress | 2026-09-21 10:58 PT: resolution Done; status In Progress to Done; Fix Version 0.4.2

### PHO-93: fix: Settings sidebar focus ring stays stuck on one row

Bug, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.3. Labels: github, settings.

Created 2026-09-21 11:33 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 12:08 PT.
- relates to PHO-108
- GitHub: [ryan-stoffel/photon#202: fix: Settings sidebar focus ring stays stuck on one row](https://github.com/ryan-stoffel/photon/issues/202)

**Problem**

In the Photon Settings window, a blue focus box appears on a sidebar row and does not move or clear when pressing Tab.

**Expected**

- Tab moves keyboard focus through the Settings window.
- The highlight does not stay stuck on one sidebar row.
- Photon Settings chrome from 0.4.2 stays (materials, 12 pt corners, sidebar rows, Command-comma).

**Out of scope**

Siri.

History: 2026-09-21 11:33 PT: Sprint PHO Sprint 2 | 2026-09-21 11:35 PT: status To Do to In Progress | 2026-09-21 12:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.3

### PHO-108: fix: Settings sidebar focus ring stays on the previous row after a click

Bug, Done (Done), priority Medium. Sprint: PHO Sprint 3. Fix version: 0.4.8. Labels: github, settings.

Created 2026-09-29 09:49 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-29 12:39 PT.
- relates to PHO-93
- GitHub: [ryan-stoffel/photon#238: fix: Settings sidebar focus ring stays on the previous row after a click](https://github.com/ryan-stoffel/photon/issues/238)
- GitHub: [ryan-stoffel/photon#239: fix(settings): move the sidebar focus ring on click](https://github.com/ryan-stoffel/photon/pull/239)

**Problem**

In the Photon Settings window, the blue focus ring on a sidebar row only moves when Tab is pressed. A mouse click changes the selected page (and its gray highlight) but leaves the blue ring on the previous row. For example, General can still show the ring after Clipboard is clicked.

**Expected**

- Clicking a sidebar row moves the blue focus ring to that row.
- Tab still moves the ring.
- Settings chrome stays as it is (materials, corners, row layout, Command-comma).

**Actual**

The ring stays on the previously focused row after a click.

**Out of scope**

Siri.

History: 2026-09-29 09:50 PT: Sprint PHO Sprint 3 | 2026-09-29 09:50 PT: status To Do to In Progress | 2026-09-29 12:39 PT: resolution Done; status In Progress to Done; Fix Version 0.4.8

## Epic PHO-7: Release and CI (22 issues)

### PHO-7: Release and CI

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-13 19:45 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 20:22 PT.

CI, parity harness, signing, Homebrew cask, releases.

History: 2026-09-13 19:51 PT: status To Do to In Progress | 2026-09-28 20:22 PT: resolution Done; status In Progress to Done

### PHO-15: CI pipeline

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.0. Labels: ci, github.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 19:56 PT.
- GitHub: [ryan-stoffel/photon#8: CI pipeline](https://github.com/ryan-stoffel/photon/issues/8)

**Summary**

GitHub Actions CI on PRs and pushes to `develop` / `main`: branch-name check, SwiftLint + SwiftFormat, Release build, unit tests, upload `Photon.app`. Jobs are the required status checks.

**Acceptance criteria**

- `.github/workflows/ci.yml` exists
- Jobs named `branch-name`, `lint`, `build`, `test`
- `Photon.app` is uploaded as an artifact
- SwiftPM `.build` is cached
- Required on `develop` and `main`

> **Ryan Stoffel** 2026-09-13 19:56 PT: CI jobs branch-name, lint, build, and test are green on macos-latest (see PR #15).

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 19:52 PT: status To Do to In Progress | 2026-09-13 19:56 PT: resolution Done; status In Progress to Done; Fix Version 0.1.0

### PHO-16: Release pipeline

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: github, release.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 21:53 PT.
- GitHub: [ryan-stoffel/photon#9: Release pipeline](https://github.com/ryan-stoffel/photon/issues/9)
- GitHub: [ryan-stoffel/photon#21: ci(release): dry-run mode, launch smoke test, and release notes](https://github.com/ryan-stoffel/photon/pull/21)
- GitHub: [ryan-stoffel/photon#23: ci(release): dry-run mode, launch smoke test, and release notes](https://github.com/ryan-stoffel/photon/pull/23)

**Summary**

Tag `v*.*.*` builds a Release, signs/notarizes when Apple secrets exist (otherwise ad-hoc), publishes `Photon-<version>.zip`, `Photon-<version>.dmg`, `SHA256SUMS`, and a GitHub Release. Does not cut a release in Phase 1.

**Acceptance criteria**

- `.github/workflows/release.yml` runs on `v*.*.*`
- Tag must match the `VERSION` file
- Ad-hoc path is documented in release notes when secrets are missing
- `docs/releasing.md` lists secrets and the procedure
- `Scripts/bump-version.sh` is the only way to change the version string

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 21:18 PT: status To Do to In Progress | 2026-09-13 21:53 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-17: Homebrew cask in ryanstoffel/taps

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: github, release.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 21:53 PT.
- GitHub: [ryan-stoffel/photon#10: Homebrew cask in ryanstoffel/taps](https://github.com/ryan-stoffel/photon/issues/10)

**Summary**

The release workflow bumps `Casks/photon.rb` in [RyanStoffel/homebrew-taps](https://github.com/RyanStoffel/homebrew-taps) when `HOMEBREW_TAP_TOKEN` is set. Install line: `brew install --cask ryanstoffel/taps/photon`.

**Acceptance criteria**

- Cask token is `photon`
- URL is `Photon-#{version}.zip` from the GitHub Release
- `Scripts/update-homebrew-cask.sh` updates version + sha256
- Missing tap token skips the bump without failing the release

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 19:52 PT: status To Do to In Progress | 2026-09-13 21:53 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-18: Developer ID signing and notarization

Task, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.4.8. Labels: github, release.

Created 2026-09-13 19:50 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 20:17 PT.
- GitHub: [ryan-stoffel/photon#11: Developer ID signing and notarization](https://github.com/ryan-stoffel/photon/issues/11)

**Summary**

Ryan supplies Apple secrets. When they are present, the release workflow signs with Developer ID, notarizes, and staples. Until then, builds are ad-hoc signed.

**Acceptance criteria**

- Secrets documented in `docs/releasing.md`
- `Scripts/sign-and-package.sh` uses them when set
- Release notes state the signing mode
- Ryan has added the secrets (blocked on Ryan)

> **Ryan Stoffel** 2026-09-13 21:51 PT: v0.1.0 shipped ad-hoc signed. The workflow is ready for Developer ID signing, notarization, and automatic cask bumps as soon as these repository secrets exist (**Settings > Secrets and variables > Actions**). Nothing else needs to change; the next `v*.*.*` tag picks them up. Details and how to obtain each value: [docs/releasing.md](https://github.com/RyanStoffel/photon/blob/develop/docs/releasing.md#secrets-settings--secrets-and-variables--actions). Signing (both required for a Developer ID signed build): - `MACOS_CERTIFICATE_P12`: base64 of the exported Developer ID Application `.p12` (`base64 -i cert.p12 | pbcopy`) - `MACOS_CERTIFICATE_PASSWORD`: password chosen for that `.p12` Notarization (all three, in addition to the signing pair): - `APPLE_ID`: developer account email - `APPLE_TEAM_ID`: 10-character Team ID from developer.apple.com/account - `APPLE_APP_SPECIFIC_PASSWORD`: app-specific password from account.apple.com for `notarytool` Cask automation: - `HOMEBREW_TAP_TOKEN`: fine-grained PAT scoped to `RyanStoffel/homebrew-taps` with Contents: read and write Verification once set: run **Actions > Release > Run workflow** (dry run). The `Sign, notarize, package` step should report `signing=developer-id-notarized`, and `RELEASE_NOTES.md` in the `Photon-<version>-dry-run` artifact should say the build is notarized and stapled. Then tag the next release.

> **Ryan Stoffel** 2026-09-28 20:17 PT: v0.4.7 is the first notarized pre-release. The tag workflow reported `signing=developer-id-notarized`, published the GitHub Release, and bumped the Homebrew cask to 0.4.7. The release notes say the build is signed with a Developer ID certificate, notarized by Apple, and stapled. - Release: https://github.com/ryan-stoffel/photon/releases/tag/v0.4.7 - Actions: https://github.com/ryan-stoffel/photon/actions/runs/36515748028

History: 2026-09-13 19:50 PT: Sprint PHO Sprint 1 | 2026-09-13 19:52 PT: status To Do to In Progress | 2026-09-28 20:17 PT: resolution Done; status In Progress to Done; Fix Version 0.4.8

### PHO-24: CI screenshot harness for UI scenarios

Task, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.1.1. Labels: ci, github.

Created 2026-09-13 23:04 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-13 23:20 PT.
- GitHub: [ryan-stoffel/photon#34: CI screenshot harness for UI scenarios](https://github.com/ryan-stoffel/photon/issues/34)
- GitHub: [ryan-stoffel/photon#35: feat(ci): UI scenario screenshot harness](https://github.com/ryan-stoffel/photon/pull/35)

Automated macOS screenshots of deterministic UI scenarios for agent/human visual QA. See PR.

History: 2026-09-13 23:04 PT: Sprint PHO Sprint 1 | 2026-09-13 23:05 PT: status To Do to In Progress | 2026-09-13 23:20 PT: resolution Done; status In Progress to Done; Fix Version 0.1.1

### PHO-57: v0.3.4: Extend macOS parity harness for v0.3.4 gates

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.4.7. Labels: github, release.

Created 2026-09-16 20:58 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:08 PT.
- blocks PHO-58
- GitHub: [ryan-stoffel/photon#132: v0.3.4: Extend macOS parity harness for v0.3.4 gates](https://github.com/ryan-stoffel/photon/issues/132)
- GitHub: [ryan-stoffel/photon#137: fix(files): reset recents when reopening Files mode](https://github.com/ryan-stoffel/photon/pull/137)
- GitHub: [ryan-stoffel/photon#139: fix(parity): stabilize Files recents gate](https://github.com/ryan-stoffel/photon/pull/139)

Mixed search Files UI, guide math, real paste sentinel, screenshots.

> **Ryan Stoffel** 2026-09-28 10:08 PT: Shipped in v0.3.4. The packaged macOS gate covers mixed-bar Files promotion, guide span, and a real paste sentinel. See the 0.3.4 changelog and release.

History: 2026-09-16 20:58 PT: Sprint PHO Sprint 1 | 2026-09-16 21:40 PT: status To Do to In Progress | 2026-09-28 10:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-58: Release Photon v0.3.4

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.4. Labels: github, release.

Created 2026-09-16 21:25 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-16 21:51 PT.
- is blocked by PHO-54
- is blocked by PHO-55
- is blocked by PHO-56
- is blocked by PHO-57
- GitHub: [ryan-stoffel/photon#134: Release Photon v0.3.4](https://github.com/ryan-stoffel/photon/issues/134)
- GitHub: [ryan-stoffel/photon#135: Release v0.3.4](https://github.com/ryan-stoffel/photon/pull/135)
- GitHub: [ryan-stoffel/photon#136: Release v0.3.4](https://github.com/ryan-stoffel/photon/pull/136)
- GitHub: [ryan-stoffel/photon#138: chore: back-merge main after v0.3.4](https://github.com/ryan-stoffel/photon/pull/138)
- GitHub: [ryan-stoffel/photon#140: chore: back-merge main after v0.3.4 hotfix](https://github.com/ryan-stoffel/photon/pull/140)

Regression fix release for file search, drag guides, clipboard paste, and parity harness.

History: 2026-09-16 21:25 PT: Sprint PHO Sprint 1 | 2026-09-16 21:25 PT: status To Do to In Progress | 2026-09-16 21:51 PT: resolution Done; status In Progress to Done; Fix Version 0.3.4

### PHO-61: v0.3.5: Extend macOS parity for Ryan-like Documents path

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.5. Labels: github, release.

Created 2026-09-16 22:26 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-16 23:16 PT.
- blocks PHO-62
- GitHub: [ryan-stoffel/photon#143: v0.3.5: Extend macOS parity for Ryan-like Documents path](https://github.com/ryan-stoffel/photon/issues/143)

Gate ember PDF under Documents/School/Capstone without mdimport-only reliance.

History: 2026-09-16 22:27 PT: Sprint PHO Sprint 1 | 2026-09-16 22:28 PT: status To Do to In Progress | 2026-09-16 23:16 PT: resolution Done; status In Progress to Done; Fix Version 0.3.5

### PHO-62: Release v0.3.5

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.4.7. Labels: github, release.

Created 2026-09-16 23:16 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:08 PT.
- is blocked by PHO-59
- is blocked by PHO-60
- is blocked by PHO-61
- GitHub: [ryan-stoffel/photon#145: Release v0.3.5](https://github.com/ryan-stoffel/photon/issues/145)
- GitHub: [ryan-stoffel/photon#146: Release v0.3.5](https://github.com/ryan-stoffel/photon/pull/146)
- GitHub: [ryan-stoffel/photon#147: Release v0.3.5](https://github.com/ryan-stoffel/photon/pull/147)

Ship vertical expansion and file search fixes.

> **Ryan Stoffel** 2026-09-28 10:08 PT: Shipped as v0.3.5 (vertical expansion and the file-search fixes in that release). Later versions through v0.4.6 are on Homebrew.

History: 2026-09-16 23:17 PT: Sprint PHO Sprint 1 | 2026-09-16 23:16 PT: status To Do to In Progress | 2026-09-28 10:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-66: fix: CI smoke native-parity SIGTERM after Files recents wait

Bug, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.3.6. Labels: ci, github.

Created 2026-09-17 07:20 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 07:48 PT.
- GitHub: [ryan-stoffel/photon#152: fix: CI smoke native-parity SIGTERM after Files recents wait](https://github.com/ryan-stoffel/photon/issues/152)

**Area**

ci

**Photon version**

0.3.5 (example: Actions run 355 on \`fix: v0.3.5 vertical expansion and real-world file search\`)

**macOS version**

macos-latest (GitHub Actions)

**Expected**

The packaged \`smoke\` job stays green. After mixed \`ember\` Files promotion, empty Files mode loads recents (PDF + PNG). Cleanup must not turn a passed (or clearly timed-out) harness into a confusing SIGTERM exit.

**Actual**

Dump shows Files **did** find \`Ember_Individual_Pitch.pdf\` (\`fileStatus: results\`, \`fileSelectedName\` correct, \`fileResultCount: 1\`). Then:

\`\`\` Scripts/check-native-parity.sh: line 49: 1672 Terminated: 15  native-paste-target ... Scripts/check-native-parity.sh: line 49: 1678 Terminated: 15  Photon ... Error: Process completed with exit code 1. \`\`\`

Real failure (run 355): \`Timed out: empty Files mode shows seeded recents and selects the PDF\`.

Launcher \`query\` is empty while \`fileControllerQuery\` is still \`ember\`. \`showFiles:\` (empty) does not clear grant-resume protection, so recents never replace the previous one-hit results list. EXIT trap then SIGTERMs Photon and the paste helper; \`set -e\` prints those Terminated lines.

Also seen: \`frame.y\` = \`-216\` (panel left off-screen after drag checks).

**Steps to reproduce**

- Run \`Scripts/check-native-parity.sh\` on macos-latest after mixed ember promotion.
- Empty Files recents gate waits 35s for PDF+PNG while controller query stays \`ember\`.

**Acceptance**

- Empty Files session after an ember search loads recents (both seeded files)
- Do not weaken assertions
- EXIT cleanup does not fail the job with SIGTERM when the harness already passed
- Real hangs (paste/files waits) still fail with the wait label, not a bare Terminated dump

History: 2026-09-17 07:21 PT: Sprint PHO Sprint 1 | 2026-09-17 07:22 PT: status To Do to In Progress | 2026-09-17 07:48 PT: resolution Done; status In Progress to Done; Fix Version 0.3.6

### PHO-67: chore: release Photon v0.3.6

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.4.7. Labels: github, release.

Created 2026-09-17 07:49 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:08 PT.
- GitHub: [ryan-stoffel/photon#154: chore: release Photon v0.3.6](https://github.com/ryan-stoffel/photon/issues/154)
- GitHub: [ryan-stoffel/photon#155: chore(release): 0.3.6](https://github.com/ryan-stoffel/photon/pull/155)

**Area**

release

**Summary**

Ship Photon v0.3.6 from develop (compact horizontal Files/clipboard split, snappy expand, clipboard list highlight, smoke recents/harness fixes).

**Acceptance**

- Version 0.3.6 on main
- GitHub Release v0.3.6 with zip/dmg
- Homebrew cask bump in RyanStoffel/homebrew-taps
- main back-merged to develop
- Packaged smoke green on the release

> **Ryan Stoffel** 2026-09-28 10:08 PT: Shipped as v0.3.6: version on main, GitHub Release with zip and dmg, Homebrew cask bump, and the back-merge onto develop. Later versions through v0.4.6 are on Homebrew.

History: 2026-09-17 07:49 PT: Sprint PHO Sprint 1 | 2026-09-17 07:50 PT: status To Do to In Progress | 2026-09-28 10:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-69: Release Photon v0.3.7

Task, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.8. Labels: github.

Created 2026-09-17 11:44 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 12:58 PT.
- GitHub: [ryan-stoffel/photon#160: Release Photon v0.3.7](https://github.com/ryan-stoffel/photon/issues/160)

Ship Photon v0.3.7 after the unified expanded-panel layout lands.

- Merge a release PR from develop to main.
- Publish the v0.3.7 GitHub release and packaged assets.
- Update the photon cask in ryan-stoffel/homebrew-taps.
- Back-merge main into develop on a legal branch without dots in the chore slug.
- Verify packaged macOS runtime checks and matching launcher/Files/clipboard screenshots.

Tracks https://github.com/ryan-stoffel/photon/issues/159.

History: 2026-09-17 11:45 PT: Sprint PHO Sprint 1 | 2026-09-17 11:46 PT: status To Do to In Progress | 2026-09-17 12:58 PT: resolution Done; status In Progress to Done; Fix Version 0.3.8

### PHO-74: Release Photon v0.3.8

Task, Done (Done), priority Medium. Sprint: PHO Sprint 1. Fix version: 0.3.9. Labels: ci, github, release.

Created 2026-09-17 13:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 16:41 PT.
- GitHub: [ryan-stoffel/photon#168: Release Photon v0.3.8](https://github.com/ryan-stoffel/photon/issues/168)

**Request**

Ship Photon v0.3.8 from `develop` onto `main`, tag `v0.3.8`, publish the GitHub Release, bump `ryan-stoffel/taps` cask `photon`, and back-merge `main` into `develop`.

Includes the v0.3.8 Files footer, keep-open folder grants, drag-anywhere, and center-snap fixes.

**Acceptance criteria**

- `VERSION` is 0.3.8 and CHANGELOG has a 0.3.8 section.
- Release workflow publishes zip + dmg and packaged smoke is green.
- Homebrew cask `ryan-stoffel/taps` photon is 0.3.8 with a matching SHA-256.
- `main` is back-merged into `develop` (chore slug with no dots).

> **Ryan Stoffel** 2026-09-17 16:41 PT: Shipped in [Photon v0.3.8](https://github.com/ryan-stoffel/photon/releases/tag/v0.3.8). Release PR #170 merged to `main` as `fe09f41`; GitHub did not auto-close because `develop` is the default branch.

History: 2026-09-17 13:28 PT: Sprint PHO Sprint 1 | 2026-09-17 13:30 PT: status To Do to In Progress | 2026-09-17 16:41 PT: resolution Done; status In Progress to Done; Fix Version 0.3.9

### PHO-77: chore: release Photon v0.3.9

Task, Done (Done), priority High. Sprint: PHO Sprint 1. Fix version: 0.4.0. Labels: github, release.

Created 2026-09-17 18:25 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-17 19:42 PT.
- GitHub: [ryan-stoffel/photon#174: chore: release Photon v0.3.9](https://github.com/ryan-stoffel/photon/issues/174)

Cut Photon **0.3.9** after the launcher recs-scroll fix and Raycast Notes revamp land on `develop`.

**Acceptance**

- `VERSION` / `PhotonVersion` / CHANGELOG section for 0.3.9
- `release/0.3.9` squash-merged into `main` with green CI
- Annotated tag `v0.3.9` and GitHub Release (zip + dmg)
- Homebrew cask `ryan-stoffel/taps` `photon` bumped (chore slug without dots, e.g. `chore/photon-v0-3-9`)
- `main` back-merged into `develop` (`chore/backmerge-v0-3-9`)
- Packaged macOS parity: Down on recs does not wrap at first page; notes window width constant
- Screenshots under the project store `media/v0.3.9/`

History: 2026-09-17 18:26 PT: Sprint PHO Sprint 1 | 2026-09-17 18:27 PT: status To Do to In Progress | 2026-09-17 19:42 PT: resolution Done; status In Progress to Done; Fix Version 0.4.0

### PHO-82: chore: release Photon v0.4.0

Task, Done (Done), priority Highest. Sprint: PHO Sprint 2. Fix version: 0.4.1. Labels: github, release.

Created 2026-09-20 23:17 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 01:07 PT.
- GitHub: [ryan-stoffel/photon#182: chore: release Photon v0.4.0](https://github.com/ryan-stoffel/photon/issues/182)
- GitHub: [ryan-stoffel/photon#185: fix: keep launcher visible during packaged drag checks](https://github.com/ryan-stoffel/photon/pull/185)

**Summary**

Ship Photon v0.4.0: foreground app launch, launcher snappiness, ⌘, Settings, and native Settings / Liquid Glass chrome.

**Acceptance criteria**

- Feature PR merged to `develop` with green CI (lint, test, build, smoke including new packaged checks).
- Release PR to `main`, tag `v0.4.0`, GitHub Release, Homebrew cask `ryan-stoffel/taps`.
- Back-merge `main` into `develop` (no dots in chore slugs).
- Screenshots in the Project store under `media/v0.4.0/`, including Settings.

History: 2026-09-20 23:17 PT: Sprint PHO Sprint 2 | 2026-09-21 00:45 PT: status To Do to In Progress | 2026-09-21 01:07 PT: resolution Done; status In Progress to Done; Fix Version 0.4.1

### PHO-85: chore: release Photon v0.4.1

Task, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.2. Labels: github, release.

Created 2026-09-21 07:28 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 09:07 PT.
- GitHub: [ryan-stoffel/photon#189: chore: release Photon v0.4.1](https://github.com/ryan-stoffel/photon/issues/189)

**Summary**

Ship Photon v0.4.1 so Ryan can `brew upgrade --cask ryan-stoffel/taps/photon`: Dock-style running dots and trailing keybind chips.

**Acceptance criteria**

- Feature PR merged to `develop` with green CI (lint, test, build, packaged smoke including `Scripts/check-native-parity.sh`).
- Release PR to `main`, tag `v0.4.1`, GitHub Release, Homebrew cask on `ryan-stoffel/taps` (Homebrew 7: `brew tap ryan-stoffel/taps` + `brew trust`).
- Back-merge `main` into `develop` (no dots in chore slugs).
- Stills in the Project store under `media/v0.4.1/`, including running-dot and keybind-chip shots.

**Notes**

Ad-hoc sign if #11 secrets are still missing. Do not implement Siri.

History: 2026-09-21 07:29 PT: Sprint PHO Sprint 2 | 2026-09-21 07:30 PT: status To Do to In Progress | 2026-09-21 09:07 PT: resolution Done; status In Progress to Done; Fix Version 0.4.2

### PHO-90: chore: release Photon v0.4.2

Task, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.3. Labels: github, release.

Created 2026-09-21 09:41 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 11:12 PT.
- GitHub: [ryan-stoffel/photon#197: chore: release Photon v0.4.2](https://github.com/ryan-stoffel/photon/issues/197)

**Summary**

Ship Photon v0.4.2 so Ryan can `brew upgrade --cask ryan-stoffel/taps/photon`: Photon-styled Settings, Caps Lock Hyper that does not toggle Caps Lock, app-hotkeys list, and running apps first in the launcher.

**Acceptance criteria**

- Feature PR merged to `develop` with green CI (lint, test, build, packaged smoke including `Scripts/check-native-parity.sh`).
- Release PR to `main`, tag `v0.4.2`, GitHub Release, Homebrew cask on `ryan-stoffel/taps` (Homebrew 7: `brew tap ryan-stoffel/taps` + `brew trust`).
- Back-merge `main` into `develop` (no dots in chore slugs).
- Stills in the Project store under `media/v0.4.2/`: Settings (Photon-styled), app-hotkeys list, launcher with running apps on top.

**Notes**

Ad-hoc sign if #11 secrets are still missing. Do not implement Siri.

History: 2026-09-21 09:41 PT: Sprint PHO Sprint 2 | 2026-09-21 09:43 PT: status To Do to In Progress | 2026-09-21 11:12 PT: resolution Done; status In Progress to Done; Fix Version 0.4.3

### PHO-96: chore: release Photon v0.4.3

Task, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.4. Labels: github, release.

Created 2026-09-21 11:33 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 14:18 PT.
- GitHub: [ryan-stoffel/photon#205: chore: release Photon v0.4.3](https://github.com/ryan-stoffel/photon/issues/205)

**Expected**

Ship 0.4.3 so `brew upgrade` picks it up.

- Squash-merge the feature work into `develop` after green packaged macOS smoke (`Scripts/check-native-parity.sh`).
- Release PR into `main`, tag `v0.4.3`, GitHub Release, Homebrew cask on `ryan-stoffel/taps`.
- Ad-hoc sign if the Apple secrets on #11 are still missing.
- Keep files, clipboard, notes, drag, ember ranking, recs scroll, foreground launch, running dots, keybind chips, Command-comma, Photon Settings chrome, Caps Lock Hyper, and the app-hotkeys list.

**Out of scope**

Siri.

History: 2026-09-21 11:33 PT: Sprint PHO Sprint 2 | 2026-09-21 11:35 PT: status To Do to In Progress | 2026-09-21 14:18 PT: resolution Done; status In Progress to Done; Fix Version 0.4.4

### PHO-99: chore: release Photon v0.4.4

Task, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.5. Labels: github, release.

Created 2026-09-21 17:37 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 18:27 PT.
- GitHub: [ryan-stoffel/photon#213: chore: release Photon v0.4.4](https://github.com/ryan-stoffel/photon/issues/213)

**Expected**

Ship 0.4.4 so `brew upgrade` picks it up.

- Squash-merge the interactive onboarding and full app icon into `develop` after green packaged macOS smoke (`Scripts/check-native-parity.sh`).
- Release PR into `main`, tag `v0.4.4`, GitHub Release, Homebrew cask on `ryan-stoffel/taps`.
- Ad-hoc sign if the Apple secrets on #11 are still missing.
- Keep Suggestions, Settings focus, first-launch Accessibility and Input Monitoring, files, clipboard, notes, drag, ember ranking, recs scroll, foreground launch, running dots, keybind chips, Command-comma, Photon Settings chrome, Caps Lock Hyper, and the app-hotkeys list.

**Out of scope**

Siri.

History: 2026-09-21 17:38 PT: Sprint PHO Sprint 2 | 2026-09-21 17:39 PT: status To Do to In Progress | 2026-09-21 18:27 PT: resolution Done; status In Progress to Done; Fix Version 0.4.5

### PHO-101: chore: release Photon v0.4.5

Task, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.6. Labels: github.

Created 2026-09-22 08:07 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-22 08:28 PT.
- GitHub: [ryan-stoffel/photon#218: chore: release Photon v0.4.5](https://github.com/ryan-stoffel/photon/issues/218)

**Expected**

Ship 0.4.5 so `brew upgrade` picks it up.

- Squash-merge the cinematic full-screen onboarding into `develop` after green packaged macOS smoke (`Scripts/check-native-parity.sh`).
- Release PR into `main`, tag `v0.4.5`, GitHub Release, Homebrew cask on `ryan-stoffel/taps`.
- Ad-hoc sign if the Apple secrets on #11 are still missing.
- Keep Suggestions, Settings focus, files, clipboard, notes, drag, ember ranking, recs scroll, foreground launch, running dots, keybind chips, Command-comma, Photon Settings chrome, Caps Lock Hyper, the app-hotkeys list, and the full app icon.

**Out of scope**

Siri.

History: 2026-09-22 08:08 PT: Sprint PHO Sprint 2 | 2026-09-22 08:09 PT: status To Do to In Progress | 2026-09-22 08:28 PT: resolution Done; status In Progress to Done; Fix Version 0.4.6

### PHO-103: chore: release Photon v0.4.6

Task, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.7. Labels: github.

Created 2026-09-22 09:14 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-22 09:31 PT.
- GitHub: [ryan-stoffel/photon#223: chore: release Photon v0.4.6](https://github.com/ryan-stoffel/photon/issues/223)

**Expected**

Ship 0.4.6 so `brew upgrade` picks it up.

- Squash-merge the windowed first-run sequence into `develop` after green packaged macOS smoke (`Scripts/check-native-parity.sh`).
- Release PR into `main`, tag `v0.4.6`, GitHub Release, Homebrew cask on `ryan-stoffel/taps`.
- Ad-hoc sign if the Apple secrets on #11 are still missing.
- Keep Suggestions, Settings focus, files, clipboard, notes, drag, ember ranking, recs scroll, foreground launch, running dots, keybind chips, Command-comma, Photon Settings chrome, Caps Lock Hyper, the app-hotkeys list, and the full app icon.

**Out of scope**

Siri. Do not change the default launcher hotkey away from Command-Space.

History: 2026-09-22 09:15 PT: Sprint PHO Sprint 2 | 2026-09-22 09:16 PT: status To Do to In Progress | 2026-09-22 09:31 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-105: Add a Photon-Dev app that can run beside the release build

Task, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.7. Labels: github.

Created 2026-09-22 12:14 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:04 PT.
- GitHub: [ryan-stoffel/photon#228: Add a Photon-Dev app that can run beside the release build](https://github.com/ryan-stoffel/photon/issues/228)
- GitHub: [ryan-stoffel/photon#229: Add a Photon-Dev app that can run beside the release build](https://github.com/ryan-stoffel/photon/pull/229)

Ryan wants the Homebrew release (`Photon.app`, `com.ryanstoffel.photon`) and a develop build open at the same time, with separate defaults.

- Product name, display name, and `.app` name: `Photon-Dev`
- Bundle id: `com.ryanstoffel.photon.dev`
- `Scripts/package_app.sh --dev` (or `PHOTON_DEV=1`) writes `build/Photon-Dev.app`
- Default `Scripts/package_app.sh` stays `build/Photon.app` with the release icon and bundle id
- Dev app icon: the existing Photon icon plus a yellow tag in the top-right that reads "dev". The release icon stays unchanged.
- Dev menu-bar status image gets the same yellow "dev" tag, since Photon is a menu-bar app

Do not change the launcher hotkey. Do not bump or retag the Homebrew cask.

History: 2026-09-22 12:14 PT: Sprint PHO Sprint 2 | 2026-09-22 12:25 PT: status To Do to In Progress | 2026-09-28 10:04 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

## Epic PHO-91: Onboarding (7 issues)

### PHO-91: Onboarding

Epic, Done (Done), priority Medium. Sprint: backlog. Fix version: none. Labels: epic.

Created 2026-09-21 11:30 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-29 13:07 PT.

First-run experience and permission prompts.

History: 2026-09-21 11:34 PT: status To Do to In Progress | 2026-09-29 13:07 PT: resolution Done; status In Progress to Done

### PHO-94: feat: request Accessibility permissions on first launch

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.3. Labels: github, keybinds.

Created 2026-09-21 11:33 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 12:08 PT.
- GitHub: [ryan-stoffel/photon#203: feat: request Accessibility permissions on first launch](https://github.com/ryan-stoffel/photon/issues/203)

**Problem**

Photon asks for Accessibility (and Input Monitoring, which Keybinds already treats as a permission macOS may request) later, when a feature first needs it. The prompts should happen up front.

**Expected**

- On first launch, request the Accessibility dialog and the Input Monitoring dialog together, before a feature trips them.
- Later Settings controls can still grant access if the first prompt was dismissed.
- Do not add new permission types Photon does not already need. Folder open panels stay user-initiated.

**Out of scope**

Siri.

History: 2026-09-21 11:33 PT: Sprint PHO Sprint 2 | 2026-09-21 11:35 PT: status To Do to In Progress | 2026-09-21 12:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.3

### PHO-95: feat: first-run walkthrough for how to use Photon

Story, Done (Done), priority High. Sprint: PHO Sprint 2. Fix version: 0.4.3. Labels: github, launcher, settings.

Created 2026-09-21 11:33 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 12:08 PT.
- GitHub: [ryan-stoffel/photon#204: feat: first-run walkthrough for how to use Photon](https://github.com/ryan-stoffel/photon/issues/204)

**Problem**

Photon has no first-run walkthrough. The first open should teach the product in a short, polished sequence in the spirit of Dia's first-run (one idea at a time, then you are in the product). Do not copy Dia branding or assets.

**Expected**

A native macOS walkthrough using Photon materials that covers:

- opening the launcher
- search
- suggestions
- clipboard, notes, and files
- Command-comma Settings

Not a settings dump.

**Out of scope**

Siri. No accounts, no cloud.

History: 2026-09-21 11:33 PT: Sprint PHO Sprint 2 | 2026-09-21 11:35 PT: status To Do to In Progress | 2026-09-21 12:08 PT: resolution Done; status In Progress to Done; Fix Version 0.4.3

### PHO-97: feat: interactive first-run walkthrough

Story, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.4. Labels: github.

Created 2026-09-21 17:21 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-21 17:43 PT.
- GitHub: [ryan-stoffel/photon#210: feat: interactive first-run walkthrough](https://github.com/ryan-stoffel/photon/issues/210)
- GitHub: [ryan-stoffel/photon#212: feat(onboarding): interactive tour and full app icon](https://github.com/ryan-stoffel/photon/pull/212)

**Area**

launcher

**Summary**

The first-run walkthrough is a static card. Replace it with a guided, animated tour you can try: launcher shortcut, suggestions, search, clipboard, notes, files, and ⌘,. Keep Skip. People who already passed the 0.4.3 tour should see this once on the upgrade. Do not copy Dia or Raycast branding.

**Acceptance criteria**

- Skip still leaves the tour
- Steps cover launcher, search, suggestions, clipboard, notes, files, and ⌘,
- At least the shortcut, search, clipboard, notes, and files steps ask you to try them
- Existing installs that finished or skipped the 0.4.3 tour see this tour once
- Siri is not part of the tour

**Notes**

Feel references: Dia's first-run (guided steps, progress, you-try-it) and Raycast's first launch. Photon materials only.

History: 2026-09-21 17:21 PT: Sprint PHO Sprint 2 | 2026-09-21 17:22 PT: status To Do to In Progress | 2026-09-21 17:43 PT: resolution Done; status In Progress to Done; Fix Version 0.4.4

### PHO-100: feat: cinematic full-screen onboarding

Story, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.5. Labels: github.

Created 2026-09-22 07:44 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-22 08:06 PT.
- relates to PHO-104
- GitHub: [ryan-stoffel/photon#216: feat: cinematic full-screen onboarding](https://github.com/ryan-stoffel/photon/issues/216)
- GitHub: [ryan-stoffel/photon#217: feat(onboarding): full-screen first-run sequence](https://github.com/ryan-stoffel/photon/pull/217)

**Summary**

Replace the 0.4.4 dialog tour with a borderless full-screen sequence: reveal, Accessibility, Input Monitoring, one screen each for search, Suggestions, clipboard, notes, files, and Settings, then the real launcher shortcut.

**Scope**

- Near-black field, Sora for onboarding text, native SwiftUI animation only.
- Beam and flash, with a crossfade when Reduce Motion is on.
- Grant triggers the real system prompt. Skip explains what will not work. Escape does not skip permission screens.
- People who finished the 0.4.3 or 0.4.4 tour see this once (`onboardingRevision` 3). Settings > General can play it again.
- Packaged smoke drives settled frames so CI is not stuck on the beam.
- No Siri. No Screen Recording prompt.

History: 2026-09-22 07:45 PT: Sprint PHO Sprint 2 | 2026-09-22 07:51 PT: status To Do to In Progress | 2026-09-22 08:06 PT: resolution Done; status In Progress to Done; Fix Version 0.4.5

### PHO-102: feat: windowed first-run sequence

Story, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.6. Labels: github.

Created 2026-09-22 08:55 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-22 09:14 PT.
- relates to PHO-104
- GitHub: [ryan-stoffel/photon#221: feat: windowed first-run sequence](https://github.com/ryan-stoffel/photon/issues/221)
- GitHub: [ryan-stoffel/photon#222: feat(onboarding): play the sequence in a window](https://github.com/ryan-stoffel/photon/pull/222)

**Expected**

Replace the 0.4.5 full-screen onboarding with a calm windowed sequence.

- Borderless, non-resizable, about 800 by 560, centered, 16 pt corners, soft shadow. The desktop stays visible.
- Reveal stays inside that window: black, a blue beam, a white flash, then the Photon mark.
- Keep Accessibility, Input Monitoring, search, Suggestions, clipboard, notes, files, and Command-comma. Escape still skips every step except permissions.
- Replace the confetti with a short muted particle burst inside the onboarding window.
- People who finished 0.4.5 see this once. Settings > General can show it again.
- Ship 0.4.6 after green packaged smoke.

**Out of scope**

Siri. Do not change the default launcher hotkey away from Command-Space.

History: 2026-09-22 08:56 PT: Sprint PHO Sprint 2 | 2026-09-22 08:58 PT: status To Do to In Progress | 2026-09-22 09:14 PT: resolution Done; status In Progress to Done; Fix Version 0.4.6

### PHO-104: Rebuild onboarding (phased)

Story, Done (Done), priority Medium. Sprint: PHO Sprint 2. Fix version: 0.4.7. Labels: github.

Created 2026-09-22 11:25 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-28 10:17 PT.
- relates to PHO-100
- relates to PHO-102
- relates to PHO-110
- GitHub: [ryan-stoffel/photon#226: Rebuild onboarding (phased)](https://github.com/ryan-stoffel/photon/issues/226)
- GitHub: [ryan-stoffel/photon#227: Phase 1 onboarding overlay](https://github.com/ryan-stoffel/photon/pull/227)
- GitHub: [ryan-stoffel/photon#230: Make the Phase 1 arrival cinematic](https://github.com/ryan-stoffel/photon/pull/230)
- GitHub: [ryan-stoffel/photon#231: Replace the Phase 1 overlay with a short welcome](https://github.com/ryan-stoffel/photon/pull/231)

Ryan, 2026-09-28: the cinematic Phase 1 overlay is scratched. Phase 2 (feature tour and permission prompts) is not in this pass.

**Welcome**

- One small native window. No space theme, beam, flash, stars, or full-screen dim.
- A few lines on how to open Photon and where Settings are, and one Continue.
- Escape or Continue dismisses it. The close button does too.
- Settings > General > Show again replays it.
- People who already passed the cinematic gate are not stuck. They see this window once if they have not seen it.
- Sora stays in `Resources/Fonts`. This window uses the system font.

**Phase 2**

Not in this pass.

> **Ryan Stoffel** 2026-09-22 11:32 PT: Phase 1 is the arrival. Phase 2 is the feature tour and permission prompts.

> **Ryan Stoffel** 2026-09-22 12:38 PT: Ryan wants the Phase 1 arrival bigger, still on this screen only. The 2pt line and short tail read as too small. This pass keeps the same structure (borderless veil, desktop still visible, Reduce Motion skips the travel, click or Return only dismisses) and turns the beam, corner haze, and stars up so the arrival feels like the Mac entering space and Photon condensing out of the light. Phase 2 stays unstarted. The launcher hotkey is unchanged.

> **Ryan Stoffel** 2026-09-28 10:08 PT: Ryan, 2026-09-28: the cinematic Phase 1 overlay is scratched. No beam, flash, stars, full-screen veil, or Desktop 2 pin. The replacement is a small native welcome window: how to open Photon, where Settings are, and one Continue. Escape or Continue dismisses it. Settings > General > Show again replays it. `hasSeenMinimalWelcome` is a new gate, so a Mac that already passed `hasCompletedPhase1Onboarding` still sees this note once. Phase 2 (feature tour and permission prompts) is not in this pass. Pull request #230 is closed without merging.

History: 2026-09-22 11:26 PT: Sprint PHO Sprint 2 | 2026-09-22 11:38 PT: status To Do to In Progress | 2026-09-28 10:17 PT: resolution Done; status In Progress to Done; Fix Version 0.4.7

### PHO-110: feat: windowed welcome with the launcher shortcut

Story, Done (Done), priority Medium. Sprint: PHO Sprint 3. Fix version: 0.4.8. Labels: github, settings.

Created 2026-09-29 09:52 PT by Ryan Stoffel, assigned to Ryan Stoffel. Resolved 2026-09-29 13:02 PT.
- relates to PHO-104
- GitHub: [ryan-stoffel/photon#242: feat: windowed welcome with the launcher shortcut](https://github.com/ryan-stoffel/photon/issues/242)
- GitHub: [ryan-stoffel/photon#245: feat(onboarding): show the launcher shortcut in a welcome window](https://github.com/ryan-stoffel/photon/pull/245)

**Summary**

The first-run note is a small titled window. Replace it with one normal window that uses Photon's panel material and 12 pt corners.

**Acceptance criteria**

- One window, not full screen and not a tiny alert. No beam, flash, star field, or SpriteKit overlay.
- Shows the Photon app icon and the name Photon.
- Shows a graphic of the launcher hotkey from the real shortcut (`HotkeyCombo.defaultCombo` when unset, the saved shortcut when the user has changed it).
- One button presents the system prompts Photon already requests, one at a time: Accessibility, then Input Monitoring. The next prompt waits until the user answers the current one.
- First launch only (`hasSeenMinimalWelcome`). Settings > General can still show it again.

**Notes**

Folder open panels stay user-initiated. No new permission types. No release tag.

History: 2026-09-29 09:53 PT: Sprint PHO Sprint 3 | 2026-09-29 09:53 PT: status To Do to In Progress | 2026-09-29 13:02 PT: resolution Done; status In Progress to Done; Fix Version 0.4.8
