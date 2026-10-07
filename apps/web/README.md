# apps/web

The Ember web app: sign-up and onboarding, the dashboard, the knowledge graph explorer, and settings. Jira: EMBER-51 (scaffold), epic EMBER-13 (dashboard and onboarding). Stack decision: [ADR 0005](../../docs/adr/0005-web-app-stack.md).

Next.js (App Router, TypeScript) serves the pages and the auth API from one Node server. Auth is [Better Auth](https://www.better-auth.com/) with email and password. GitHub OAuth (EMBER-12) and organizations/tenants are later additions. The Electron desktop app (EMBER-17) will wrap this same app; it doesn't get a second UI.

## What's in it

| Path | What it does |
| --- | --- |
| `/` | Landing page. Signed-in users go straight to the dashboard (or onboarding). |
| `/signup` | Step 1 of 2: name, email, password (at least 8 characters, with a strength meter). Signing up also logs in. |
| `/onboarding` | Step 2 of 2: a grid of the launch sources (GitHub, GitLab, Jira, Slack, Teams). Pick one or more. Anyone signed in without sources lands here. |
| `/login` | Email and password. Goes back to `?next=` (same-site paths only). |
| `/dashboard` | **Needs attention** (where the sources disagree, with the evidence from each), summary numbers, **work in flight** (each ticket with its PR and where it was last discussed), your sources, and recent activity. |
| `/graph` | The knowledge graph: drag, zoom, search, filter by subgraph and node type, click a node for its fields and connections. `?focus=<node id>` opens on one node. Every "View in graph" link uses it. |
| `/settings` | Name, sources, theme (light, dark, system), password, log out. |
| `/api/auth/*` | Better Auth's endpoints (`sign-up/email`, `sign-in/email`, `sign-out`, `get-session`, `update-user`, `change-password`, …). |

Light and dark mode follow the system until the user picks one. The choice is saved in the browser and applied before the first paint, so there's no flash.

## Sources

`src/lib/sources.ts` is the catalog of the five launch sources from `docs/graph-schema.md`. The user's pick is a Better Auth field on the `user` table (`sources`, a `string[]`: JSON text on SQLite, `jsonb` on Postgres). It's validated on the server: known ids only, at least one, deduplicated, and stored in catalog order. The dashboard and the graph only show data from the picked sources.

A user who picked only GitLab or Teams sees an empty state, because the preview has no data for those yet.

## Preview data

Nothing is connected yet, so the dashboard and the graph read **`src/data/preview-workspace.json`**. It describes a made-up team, Kestrel Labs, with:
- 6 people
- 3 GitHub repositories and 11 pull requests
- a Jira project with 17 issues, comments and status history
- 4 Slack channels with 29 messages and threads

It's written so each kind of insight shows up once or more. For example, someone says in Slack that CHK-131 is done and its PR merged, but Jira still says In Development.

The records are a simplified form of what the connectors emit, keyed the way each source keys them (GitHub login, Jira account id, Slack user id). All timestamps are shifted at load time so that `anchor` reads as "now". To change the story, edit the JSON and keep times relative to `anchor`. `test/workspace.test.ts` checks that every account, epic, link and thread points at something real.

The rest is real code that keeps working when the data comes from the graph instead:

| File | Job |
| --- | --- |
| `src/lib/workspace/load.ts` | The only place that reads the JSON. Swap this for the graph API later. |
| `src/lib/workspace/model.ts` | Resolves people across sources. Links PRs to tickets (key in title or branch). Reads what each message mentions; replies inherit their thread's subject. |
| `src/lib/workspace/text.ts` | Ticket keys, `repo#123`, PR links, Slack mentions and permalinks. Done, blocked and decision signals (keyword rules, standing in for the pipeline's extraction). |
| `src/lib/workspace/insights.ts` | The rules: blockers, failing checks, status drift, stale reviews, missing links, unrecorded decisions. Each rule needs only the sources it reads. |
| `src/lib/workspace/graph.ts` | Builds the graph in the ontology's terms: the nine node types, the relationship names from `services/pipeline/ontology/edges.py`, and `<tenant>_<source>` subgraph ids. A test checks every edge against the allowed endpoint pairs. |
| `src/lib/workspace/summary.ts` | Dashboard numbers, the work table, the activity feed. |

## Database

`DATABASE_URL` picks the database. Nothing else changes:

| `DATABASE_URL` | Database |
| --- | --- |
| `file:./data/ember.db` (default) | SQLite, through Node's built-in `node:sqlite` (no native module) |
| `postgres://user:pass@host:5432/db` | Postgres |

The auth tables (`user`, `session`, `account`, `verification`) are created or updated when the server starts (`src/instrumentation.ts`). An existing database gains the `sources` column on the next start, and its users are asked to pick sources when they next log in.

## Settings

| Variable | Required | Meaning |
| --- | --- | --- |
| `BETTER_AUTH_SECRET` | yes | Signs session cookies. Generate with `openssl rand -hex 32`. Never commit it. |
| `BETTER_AUTH_URL` | yes, outside local dev | The address users open, e.g. `http://localhost:3000`. Used for cookie and origin checks. |
| `DATABASE_URL` | no | See above. The container defaults to `file:/data/ember.db`. |

## Run

In a container (from the repo root; SQLite is kept in the `web-data` volume):

```sh
# .env needs BETTER_AUTH_SECRET (see .env.example)
docker compose -f infra/docker-compose.yml --env-file .env --profile web up --build web
# open http://localhost:3000
```

Locally, without Docker (Node 22+):

```sh
cd apps/web
npm ci
BETTER_AUTH_SECRET=dev-secret-change-me BETTER_AUTH_URL=http://localhost:3000 npm run dev
```

## Test

```sh
cd apps/web
npm run typecheck
npm test                                            # SQLite
TEST_POSTGRES_URL=postgres://user:pass@localhost:5432/ember npm test   # SQLite and Postgres
```

- `test/auth.test.ts` drives Better Auth's real handler. It covers:
  - sign-up, which also signs in
  - duplicate email and short password
  - log-in with the right and wrong password
  - log-out ending the session
  - passwords stored hashed
  - saving and validating `sources`
- `test/workspace.test.ts` covers:
  - reading references and signals out of text
  - the preview data's consistency
  - the exact insights it produces
  - filtering by source
  - the dashboard numbers
  - the graph's fit to the ontology

CI runs both against a Postgres service container.

## Conventions

- Owns only the UI and the web app's auth. Shared logic that a second consumer needs moves to `packages/` (see `packages/README.md`).
- API calls are same-origin and relative, and nothing loads from a CDN (icons and source marks are inline SVG, fonts are the system's), so the future Electron shell can load the app unchanged and offline.
- Styling is plain CSS: design tokens and shared pieces in `src/app/globals.css`, one CSS module per page or component. Colors always come from the tokens, so both themes stay in step.
- The graph layout uses `d3-force`. Everything else on the page is React and SVG.
