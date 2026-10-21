# apps/web

The Ember web app: sign-up and onboarding, the dashboard, the knowledge graph explorer, and settings. Jira: EMBER-51 (scaffold), epic EMBER-13 (dashboard and onboarding). Stack decision: [ADR 0006](../../docs/adr/0006-web-app-stack.md).

Next.js (App Router, TypeScript) serves the pages and the auth API from one Node server. Auth is [Better Auth](https://www.better-auth.com/) with email and password. GitHub OAuth (EMBER-12) and organizations/tenants are later additions. The Electron desktop app (EMBER-17) will wrap this same app; it doesn't get a second UI.

## What's in it

| Path | What it does |
| --- | --- |
| `/` | Landing page. Signed-in users go straight to the dashboard (or onboarding). |
| `/signup` | Step 1 of 2: name, email, password (at least 8 characters, with a strength meter). Signing up also logs in. With `?next=`, it goes back there instead of onboarding. |
| `/onboarding` | Step 2 of 2: a grid of the launch sources (GitHub, GitLab, Jira, Slack, Teams). Pick one or more. Anyone signed in without sources lands here. |
| `/login` | Email and password. Goes back to `?next=` (same-site paths only). |
| `/dashboard` | **Needs attention** (where the sources disagree, with the evidence from each, and a one-click fix), summary numbers, **work in flight** (each ticket with its PR and where it was last discussed), your sources, and recent activity. Everything is clickable: cards, evidence, rows, numbers and activity open a detail drawer. |
| `/graph` | The knowledge graph, one shaded region per source (GitHub, Jira, Slack) with people in the middle and the cross-source links between them. Drag, zoom, search, filter by subgraph and node type. Click a node for the same details and actions as the drawer. `?focus=<node id>` opens on one node, and every "Show in graph" link uses it. |
| `/settings` | Name, sources, preview data (reset), theme (light, dark, system), password, log out. |
| `/device` | Where `ember auth login` sends the browser: check the code matches the terminal, then approve or deny ([CLI sign-in](#cli-sign-in)). Signed-out users log in or sign up first and come back. |
| `/api/auth/*` | Better Auth's endpoints (`sign-up/email`, `sign-in/email`, `sign-out`, `get-session`, `update-user`, `change-password`, `device/*`, …). |
| `/api/whoami` | The name and email a CLI token belongs to ([CLI sign-in](#cli-sign-in)). |

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

### Acting on it

The preview works like the real product would.
- Each insight has an action: move a ticket, flag it, re-run checks, link a PR to its ticket, record a decision on its ticket, or nudge a reviewer in Slack.
- The detail drawer adds more: change a ticket's status, comment on it, reply in a thread, post in a channel.

Each action is saved as a small edit (`src/lib/workspace/edits.ts`). Edits are replayed on top of the snapshot, so insights, numbers and the graph are recalculated and a fixed problem goes away. A message offers Undo.

Edits are kept per user in the browser's localStorage (`src/components/preview/store.tsx`). Settings → Preview data resets them. Once real sources are connected, each kind of edit becomes a call to that source's API.

The rest is real code that keeps working when the data comes from the graph instead:

| File | Job |
| --- | --- |
| `src/lib/workspace/load.ts` | The only place that reads the JSON. Swap this for the graph API later. |
| `src/lib/workspace/edits.ts` | The changes a user can make, and replaying them onto a workspace. |
| `src/lib/workspace/locate.ts` | From a graph node id back to the record behind it, for the detail views. |
| `src/components/preview/` | The browser-side store, the detail drawer, the item details shared with the graph, and the Undo message. |
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

The auth tables (`user`, `session`, `account`, `verification`, `deviceCode` for CLI sign-in, and `rateLimit`) are created or updated when the server starts (`src/instrumentation.ts`), which also logs where they are, e.g. `[db] Auth database: Postgres, schema web, on hv-worker-1.tail470a31.ts.net:5432/ember`, or `SQLite file … (DATABASE_URL not set)`. The password is never logged. An existing database gains the `sources` column on the next start, and its users are asked to pick sources when they next log in.

Better Auth's rate limits are counted in the `rateLimit` table, not in memory, so every copy of the app shares them. They apply in production only: sign-in and sign-up allow 3 requests per 10 seconds per client IP, then answer `429`. The IP comes from `x-forwarded-for`, which `next start` sets itself. Behind a reverse proxy that appends to it, set `advanced.ipAddress.trustedProxies` in `src/lib/auth.ts`; otherwise Better Auth logs a warning and every client shares one limit.

On Postgres the tables live in their own schema, `web` (EMBER-80). The web app shares the `ember` database with ingestion, whose tables (`raw_events`, `source_records`, …) are in `public`, so their names never collide. Better Auth creates the schema on the first start and names it in every query, so the connection's `search_path` doesn't matter. Tables a pre-EMBER-80 build made in `public` are not moved; those were test accounts, so sign up again.

The schema keeps names apart, not access: the web app connects as `ember`, which also owns ingestion's tables. ADR 0006 plans a role of its own for the web app on the cluster.

Tables are created only when the server starts. If the database was unreachable at start, or was wiped while the app ran, requests fail (`500`) until you restart the app.

### Local Postgres

The compose `postgres` service runs the cluster's version (18.6). From the repo root:

```sh
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres up -d postgres
cd apps/web
DATABASE_URL=postgres://ember:change-me-locally@localhost:5432/ember \
  BETTER_AUTH_SECRET=dev-secret-change-me BETTER_AUTH_URL=http://localhost:3000 npm run dev
```

The password is `POSTGRES_PASSWORD` from `.env` (default `change-me-locally`). To run the web container against it instead, point `DATABASE_URL` at the service name. The `--wait` matters: the web app creates its tables at start, and a first Postgres start takes a few seconds.

```sh
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres up -d --wait postgres
DATABASE_URL=postgres://ember:change-me-locally@postgres:5432/ember \
  docker compose -f infra/docker-compose.yml --env-file .env --profile postgres --profile web up --build web
```

### Cluster Postgres

The cluster's Postgres (`ember` on `hv-worker-2`, EMBER-61) is reachable over the cluster's Tailscale tailnet only. It is wiped and recreated now and then, so keep its URL in `.env`, never in code.

1. Join the cluster's tailnet, or one the cluster's nodes are shared into. `getent hosts hv-worker-2.tail470a31.ts.net` should print an address.
2. Print the URL, with its password: `make -C infra/ansible -s postgres-url` (needs the cluster kubeconfig; see `infra/ansible`). Put it in the root `.env` as `DATABASE_URL`. Postgres answers on port 5432 of every node (`infra/k8s/apps/postgres/service-lb.yaml`), so if `hv-worker-2` isn't shared with you, use a node that is: `make -C infra/ansible -s postgres-url POSTGRES_HOST=hv-worker-1.tail470a31.ts.net`, or change the host in `.env`.
3. Run the app with that `.env`. `npm run dev` reads only `apps/web/.env*`, so load the root one into the shell first (`node --env-file` doesn't work here: Next passes it on to a worker through `NODE_OPTIONS`, which refuses it):

   ```sh
   cd apps/web
   (set -a && . ../../.env && set +a && npm run dev)
   ```

   Or run the container, which gets `.env` from compose: `docker compose -f infra/docker-compose.yml --env-file .env --profile web up --build web`. If the container can't resolve the `*.ts.net` name (some Docker DNS setups can't), use the node's `100.x` address from `tailscale status` in `DATABASE_URL`.

The `ember` user owns the database, so the first start can create the `web` schema.

## CLI sign-in

`ember auth login` (EMBER-87) signs in through the browser, the way `gh auth login` does (EMBER-81). Accounts are only created on the web app. It's the OAuth device flow ([RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)) from Better Auth's `deviceAuthorization` plugin, plus its `bearer` plugin so the token works in an `Authorization` header. Setup is in `src/lib/auth.ts`, the page in `src/app/(auth)/device/`.

Every request below is JSON (`content-type: application/json`) to the web app's address, e.g. `http://localhost:3000`. The CLI sends no cookie and no `Origin`.

1. **Ask for a code.** `POST /api/auth/device/code` with `{"client_id": "ember-cli"}`. Any other `client_id` gets `400 {"error": "invalid_client"}`.

   ```json
   {
     "device_code": "Kq3…40 characters",
     "user_code": "WDJBMJHT",
     "verification_uri": "http://localhost:3000/device",
     "verification_uri_complete": "http://localhost:3000/device?user_code=WDJBMJHT",
     "expires_in": 1800,
     "interval": 5
   }
   ```

2. **Send the user to the browser.** Open `verification_uri_complete`. Also print `user_code` as `WDJB-MJHT` (the page shows it that way, and accepts it with or without the dash) and `verification_uri`, for when no browser opens. The user signs in or creates an account, checks the code, and clicks Approve or Deny.

3. **Poll for the token.** Every `interval` seconds, `POST /api/auth/device/token` with `{"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "…", "client_id": "ember-cli"}`. Until there's an answer it's `400` with an `error`:

   | `error` | Meaning | CLI does |
   | --- | --- | --- |
   | `authorization_pending` | Not approved yet | Keep polling |
   | `slow_down` | Polled sooner than `interval` | Wait longer, keep polling |
   | `access_denied` | The user clicked Deny | Stop: "Sign-in denied" |
   | `expired_token` | 30 minutes passed | Stop: run `ember auth login` again |
   | `invalid_grant` | Unknown code, or one already used (a code gives out one token) | Stop |

   Once approved: `200 {"access_token": "…", "token_type": "Bearer", "expires_in": 604800, "scope": ""}`. The token is a normal session, good for 7 days. Store it (EMBER-87 keeps it in the config directory with `0600`). The code is deleted at this point, so reopening the link in the browser shows it as unknown.

4. **Who am I.** `GET /api/whoami` with `Authorization: Bearer <token>` answers `200 {"name": "Ada Lovelace", "email": "ada@example.com"}`, or `401 {"error": "unauthorized", …}` for a missing, wrong or expired token. Accounts have no username, so `Successfully authenticated as <username>` prints the email. `ember auth status` is the same call.

5. **Log out.** `POST /api/auth/sign-out` with `Authorization: Bearer <token>` and body `{}` ends the session on the server (`200 {"success": true}`). Then delete the stored token.

The whole flow with `curl`, against `npm run dev`:

```sh
B=http://localhost:3000
curl -s -X POST $B/api/auth/device/code -H 'content-type: application/json' -d '{"client_id":"ember-cli"}'
# open the verification_uri_complete it prints, sign in, Approve, then:
curl -s -X POST $B/api/auth/device/token -H 'content-type: application/json' \
  -d '{"grant_type":"urn:ietf:params:oauth:grant-type:device_code","device_code":"<device_code>","client_id":"ember-cli"}'
curl -s $B/api/whoami -H 'authorization: Bearer <access_token>'
```

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
TEST_POSTGRES_URL=postgres://ember:change-me-locally@localhost:5432/ember_test npm test   # SQLite and Postgres
```

Give the tests a database of their own: they leave their test accounts behind. With the compose Postgres, create it once with `docker exec ember-postgres createdb -U ember ember_test`. Never point `TEST_POSTGRES_URL` at the cluster.

- `test/auth.test.ts` drives Better Auth's real handler. It covers:
  - sign-up, which also signs in
  - duplicate email and short password
  - log-in with the right and wrong password
  - log-out ending the session
  - passwords stored hashed
  - saving and validating `sources`
  - CLI sign-in, called the way the CLI calls it: a token after approval and `whoami` naming the user; a used, denied or expired code; polling too fast; an unknown `client_id`; a made-up token; logging out with the token; and the `/device` page showing a request only to the user who opened it
- `test/workspace.test.ts` covers:
  - reading references and signals out of text
  - the preview data's consistency
  - the exact insights it produces
  - filtering by source
  - the dashboard numbers
  - the graph's fit to the ontology
  - that each insight's action resolves it, and only it

CI runs both against a Postgres service container.

## Conventions

- Owns only the UI and the web app's auth. Shared logic that a second consumer needs moves to `packages/` (see `packages/README.md`).
- API calls are same-origin and relative, and nothing loads from a CDN (icons and source marks are inline SVG, fonts are the system's), so the future Electron shell can load the app unchanged and offline.
- Styling is plain CSS: design tokens and shared pieces in `src/app/globals.css`, one CSS module per page or component. Colors always come from the tokens, so both themes stay in step.
- The graph layout uses `d3-force`. Everything else on the page is React and SVG.
