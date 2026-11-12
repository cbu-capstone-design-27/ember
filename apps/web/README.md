# apps/web

The Ember web app: sign-up and onboarding, the dashboard, the knowledge graph explorer, and settings. Jira: EMBER-51 (scaffold), epic EMBER-13 (dashboard and onboarding). Stack decision: [ADR 0006](../../docs/adr/0006-web-app-stack.md).

Next.js (App Router, TypeScript) serves the pages and the auth API from one Node server. Auth is [Better Auth](https://www.better-auth.com/) with email and password, plus [organizations](#organizations) (EMBER-124). GitHub OAuth (EMBER-12) is a later addition. The Electron desktop app (EMBER-17) will wrap this same app; it doesn't get a second UI.

## What's in it

| Path | What it does |
| --- | --- |
| `/` | Landing page. Signed-in users go straight to the dashboard (or onboarding). |
| `/signup` | Step 1 of 2: name, email, password (at least 8 characters, with a strength meter). Signing up also logs in. With `?next=`, it goes back there instead of onboarding. |
| `/onboarding` | Step 2 of 2: a grid of the launch sources (GitHub, GitLab, Jira, Slack, Teams). Pick one or more. Anyone signed in without sources lands here. |
| `/login` | Email and password. Goes back to `?next=` (same-site paths only). |
| `/dashboard` | The active organization's real records, read from `source_records` for its tenant ([Dashboard data](#dashboard-data)): open pull requests, open issues and the last week's messages, the newest activity with a link to each original, and a record count and last activity per source. A tenant with no records, or a server on SQLite, gets an empty state. |
| `/graph` | The knowledge graph, one shaded region per source (GitHub, Jira, Slack) with people in the middle and the cross-source links between them. Drag, zoom, search, filter by subgraph and node type. Click a node for the same details and actions as the drawer. `?focus=<node id>` opens on one node, and every "Show in graph" link uses it. |
| `/organizations` | Create or join an organization. Anyone signed in who belongs to none lands here. Lists invites sent to your email and the organizations you're in ([Organizations](#organizations)). |
| `/invite/<id>` | An invite link: log in or sign up, then accept or decline. Only the invited email can. |
| `/settings` | The organization (name, slug, tenant, your role), its [connections](#connections), its members, invites (owners and admins), the linked tenant (Ember admins), then name, sources, preview data (reset), password, log out. |
| `/device` | Where `ember auth login` sends the browser: check the code matches the terminal, then approve or deny ([CLI sign-in](#cli-sign-in)). Signed-out users log in or sign up first and come back. |
| `/api/auth/*` | Better Auth's endpoints (`sign-up/email`, `sign-in/email`, `sign-out`, `get-session`, `update-user`, `change-password`, `device/*`, `organization/*`, …). |
| `/api/whoami` | The name, email and organizations a CLI token belongs to ([CLI sign-in](#cli-sign-in)). |

Light and dark mode follow the system setting (`prefers-color-scheme`), in CSS only, so there's no flash.

## Sources

`src/lib/sources.ts` is the catalog of the five launch sources from `docs/graph-schema.md`. The user's pick is a Better Auth field on the `user` table (`sources`, a `string[]`: JSON text on SQLite, `jsonb` on Postgres). It's validated on the server: known ids only, at least one, deduplicated, and stored in catalog order. The graph only shows preview data from the picked sources. The dashboard shows whatever its tenant holds; the sources card also lists the picked ones that have no records yet.

A user who picked only GitLab or Teams sees an empty graph, because the preview has no data for those yet.

## Preview data

The graph (and the drawer and actions it shares with the preview) reads **`src/data/preview-workspace.json`**. The dashboard no longer does (EMBER-125). It describes a made-up team, Kestrel Labs, with:
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
| `src/lib/workspace/summary.ts` | The preview's dashboard numbers, work table and activity feed. Nothing shows them now; they return with ticket-to-PR linking. |

## Dashboard data

`src/lib/source-records.ts` is the one module that reads ingestion's `source_records`, and it only reads. `loadDashboard(pool, tenant)` returns the summary numbers, the sources card and the activity feed for one tenant, and the API from EMBER-115 can wrap it as it is. Every query filters on the tenant (the active organization's, never one from the request), and `test/source-records.test.ts` shows one organization can't see another's records.

- **One row per object version.** A row is unique on `(source, tenant, object_id, version)`, so everything reads the newest version of each object, ordered by the source's own time (`occurred_at`, then `id`), not by arrival ([EMBER-63](../../docs/ingestion/architecture.md#source-ordering-value-ember-63)). An object whose newest version is a tombstone (`record.tombstone` on GitHub and GitLab, `record.deleted` on Jira and Slack) is gone.
- **Open** means each source's own words: GitHub `open`, GitLab `opened`, and for Jira any status category but `done`. A pull request GitHub also sends as an issue counts once, as a pull request. Messages are Slack's, from the last 7 days.
- **Activity** shows pull and merge requests, issues, reviews, comments and messages. Commits, refs, labels, users and channels count toward a source's records but aren't activity. A message links to its channel: Slack's permalink needs the workspace domain, which a record doesn't hold.
- **Not shown yet:** "Needs attention" and "Work in flight" need tickets linked to pull requests. The code for them (`insights.tsx`, `src/lib/workspace/`) stays until linking exists.

Tests run against Postgres when `TEST_POSTGRES_URL` is set, with the ingestion migrations applied (CI's web job does both). The query reads every version of the tenant's rows to find the newest; if a tenant grows large enough for that to show, keep the current state in a table of its own rather than adding indexes to this one.

## Database

`DATABASE_URL` picks the database. Nothing else changes:

| `DATABASE_URL` | Database |
| --- | --- |
| `file:./data/ember.db` (default) | SQLite, through Node's built-in `node:sqlite` (no native module) |
| `postgres://user:pass@host:5432/db` | Postgres |

The auth tables (`user`, `session`, `account`, `verification`, `deviceCode` for CLI sign-in, `rateLimit`, and `organization`, `member` and `invitation`) are created or updated when the server starts (`src/instrumentation.ts`), which also logs where they are, e.g. `[db] Auth database: Postgres, schema web, on hv-worker-1.tail470a31.ts.net:5432/ember`, or `SQLite file … (DATABASE_URL not set)`. The password is never logged. An existing database gains the `sources` column on the next start, and its users are asked to pick sources when they next log in.

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

`ember auth login` (EMBER-87) signs in through the browser, the way `gh auth login` does (EMBER-81). Accounts are only created on the web app. It's the OAuth device flow ([RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)) from Better Auth's `deviceAuthorization` plugin. The request side of its `bearer` plugin makes the token work in an `Authorization` header. Its response side is left out, because it would copy browser session tokens into a header page scripts can read. Setup is in `src/lib/auth.ts`, the page in `src/app/(auth)/device/`. Opening the page binds the code to no one: the first account to click Approve or Deny claims it, so a user signed into the wrong account can use **Use another account** first.

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

   Once approved: `200 {"access_token": "…", "token_type": "Bearer", "expires_in": 604800, "scope": ""}`. The token is a normal session. It stops working after 7 days without use, and any use more than a day after its last refresh (`whoami`, for one) moves that to 7 days from then, so a CLI in regular use stays signed in. When a call answers 401, the CLI asks for `ember auth login` again. Store the token (EMBER-87 keeps it in the config directory with `0600`). The code is deleted at this point, so reopening the link in the browser shows it as unknown.

4. **Who am I.** `GET /api/whoami` with `Authorization: Bearer <token>` answers `200 {"name": "Ada Lovelace", "email": "ada@example.com", "organizations": [{"name": "Kestrel Labs", "slug": "kestrel-labs", "tenant": "ember-development", "role": "owner"}], "active": "kestrel-labs"}`, or `401 {"error": "unauthorized", …}` for a missing, wrong or expired token. Accounts have no username, so `Successfully authenticated as <username>` prints the email. `ember auth status` is the same call, and prints the `active` organization. `active` is the slug of the organization this session works in, or `null` when the user is in none. The CLI's session is its own, so it is the first organization the user joined.

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

## Organizations

An organization is a project (EMBER-124). Its members see the same data, and a person can belong to several. They come from Better Auth's [organization plugin](https://www.better-auth.com/docs/plugins/organization), with roles `owner`, `admin` and `member`. `src/lib/organizations.ts` adds Ember's rules on top.

- **Tenant.** Each organization owns one tenant, the id ingestion files its records under (`raw_events.tenant`, `source_records.tenant`, the `{tenant}_{source}` subgraphs). It's the `tenant` column on `organization`, unique. A new organization's tenant is its slug, so slugs follow the tenant rules (`[a-z0-9][a-z0-9-]*`, at most 63 characters, `src/lib/tenant.ts`) and can't change after creation. A slug is refused if another organization holds that tenant or, on Postgres, if ingestion already has data under it (`installations`, `raw_events` or `source_records`). That data can only be reached by linking.
- **Creating.** Anyone signed in can create one at `/organizations` and becomes its owner. A user in no organization is sent there from every page of the signed-in app.
- **Inviting.** Owners and admins invite by email in Settings. Ember doesn't send email yet, so the inviter copies the link (`/invite/<id>`) and sends it. The invite also shows on `/organizations` when someone with that email logs in. Only an account with the invited email can accept, invites last a week, and owners and admins can revoke them. Re-inviting an email replaces its open invite.
- **Switching.** The sidebar's menu switches between your organizations. The choice is the session's `activeOrganizationId`, so it persists. If it's unset, or you've left that organization, the first one you joined is active.

Emails aren't verified yet, so anyone who signs up with an invited address can accept that invite. The invite email follow-up should add verification.

### Linking a tenant

To give an organization data that's already stored, such as the team's `ember-development`, an Ember admin (an email in `EMBER_ADMIN_EMAILS`) sets its tenant in Settings → Linked tenant. The slug stays the same. A tenant belongs to one organization at most. Only admins can link, because a link hands over every record under that tenant.

## Connections

Settings → Connections lists every source account feeding the organization's tenant, one per `installations` row (EMBER-147, [ADR 0010](../../docs/adr/0010-connecting-sources.md)). Each shows its source, label, who connected it (a member, "a former member", or "an Ember operator" for rows `make map-tenant` wrote) and whether it is connected. Owners and admins see Connect and Disconnect. Members see the list.

**Disconnect** (`settings/connection-actions.ts`, `disconnectAccount` in `src/lib/connections.ts`) asks first, then in one transaction:

1. Sets `disconnected_at`. Intake maps only connected accounts, so from the next delivery on the account's events are stored with no tenant, as an unmapped account's are, and enrich skips them.
2. Deletes the account's `source_credentials`.

What was already ingested stays (EMBER-53). The tenant comes from the caller's membership, never from the form. Connecting the same account to the same organization again clears `disconnected_at`. Another organization still can't take it.

Removing the source's own hook is per source, and happens before the credential is deleted. Jira deletes its dynamic webhook with the stored token ([Connect Jira](#connect-jira)), and Slack uninstalls the app with `apps.uninstall` ([Connect Slack](#connect-slack)). GitHub's uninstall needs the App's private key, which the web app doesn't hold, so after a GitHub disconnect Settings links the owner to GitHub to uninstall `ember-ingest` there. Until they do, GitHub keeps sending events, which intake stores with no tenant.

### Source credentials

Credentials an owner connects with (a Jira token, a Slack bot token, a GitLab access token) go in ingestion's `source_credentials` table, sealed with AES-256-GCM by `src/lib/source-credentials.ts` (`storeCredential`, `loadCredential`). The Python workers open them with [`packages/source-credentials`](../../packages/source-credentials/README.md), which documents the format. The web app never shows a stored credential again.

## Connect GitHub

An organization's owners and admins connect GitHub in Settings → Connections (EMBER-130). This follows [ADR 0010](../../docs/adr/0010-connecting-sources.md). Connecting maps an installation of the `ember-ingest` App to the organization's tenant in ingestion's `installations` table. It then hands the deliveries GitHub already sent for it to enrich, with no `make map-tenant`. Members see what is connected.

- **Connect GitHub** (`/connect/github`) sends the owner to install the App, with a `state` that a signed, 15-minute cookie ties to this browser, user and organization.
- **Connect an existing installation** (`/connect/github?existing=1`) signs them in to GitHub instead, then lists the App's installations they can see (`/connect/github/pick`).
  - They can tick only the ones they administer: their own personal account, or an organization they own.
  - Installations another organization holds are shown, but can't be ticked.
  - This path covers an install approved later by an organization owner, an install started on GitHub's own page, and installations that existed before this flow.
- **The callback** (`/connect/github/callback`) checks ownership:
  1. It trades GitHub's `code` for a user access token.
  2. It accepts the installation only if `GET /user/installations` lists it for that user. The `installation_id` on the redirect alone can be spoofed.
  3. It revokes the token.
  4. It writes the row with the GitHub login as `label` and the account id as `external_id`.
  5. It gives the installation's unmapped `raw_events` the tenant and notifies `raw_events`.
  6. It does that hand-off once more 10 s later, for a delivery intake was storing at that moment (`src/lib/connections.ts`).
- **An account never moves to another organization.** Connecting an installation another tenant holds is refused. So is a reinstall (a new installation id) of a GitHub account another tenant holds, matched by `external_id`. Connecting one's own disconnected installation again reconnects it.

It needs Postgres with the ingestion migrations, and the App's OAuth client in `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET`. Without them Settings says what is missing.

### GitHub App settings

On `ember-ingest`'s settings page (Settings → Developer settings → GitHub Apps, on the account or organization that owns the App):

1. **Callback URL:** `https://pi-cp.tail470a31.ts.net/connect/github/callback`, which is `BETTER_AUTH_URL` plus the path. It is a browser redirect, so a tailnet address works. The connect routes move a visitor on another node's address to this one first, because the session cookie belongs to one host.
2. **Request user authorization (OAuth) during installation:** on. GitHub then sends installs to the Callback URL with a `code`, and the Setup URL field is disabled.
3. **Permissions → Organization → Members:** read. Without it, organization installations can't be picked on the existing-installation path, and the picker says why. Changing a permission asks every installation to accept it.
4. **Client secrets → Generate a new client secret.** Put the Client ID and the secret in the vault as `vault_web_github_client_id` and `vault_web_github_client_secret`. Then run `make web-secret`, commit `infra/k8s/apps/web/auth.sops.yaml`, and restart the web app.

For local dev, a second GitHub App with Callback URL `http://localhost:3000/connect/github/callback` keeps the shared App's settings alone.

## Connect Jira

An organization's owners and admins connect a Jira site in Settings → Connections (EMBER-131, [ADR 0010](../../docs/adr/0010-connecting-sources.md#jira-ember-131)). Ember uses an Atlassian OAuth 2.0 (3LO) app, not a site admin webhook:

1. **Connect Jira** (`/connect/jira`) sends the owner to Atlassian's consent screen, with a `state` that a signed, 15-minute cookie ties to this browser, user and organization.
2. **The callback** (`/connect/jira/callback`) trades Atlassian's `code` for an access and refresh token, and checks that every scope in `SCOPES` was granted. It then lists the Jira sites the consent covers (accessible-resources) and each site's projects. Atlassian lists only sites the consenting user can use, which is the proof of ownership. A site another organization holds is shown but can't be connected.
3. **The tokens wait server side.** They are sealed with the api key ring into Better Auth's `verification` table for 15 minutes, and the picker cookie only names the row. They never reach the browser.
4. **The picker** (`/connect/jira/pick`) lists the sites and their projects. The owner ticks projects and connects one site. Then the web app (`connectSite` in `src/lib/jira-connect.ts`):
   1. Registers one dynamic webhook on the site for the events in `WEBHOOK_EVENTS`, filtered by `project in (<ids>)`, pointing at intake (`JIRA_WEBHOOK_URL`).
   2. Maps the site's host to the tenant in `installations`, with the site's cloud id as `external_id`, and runs the hand-off. A site another tenant holds is refused, and the new webhook is deleted again.
   3. Seals the token pair, the cloud id, the webhook ids and the JQL as the site's `api` credential. [`docs/ingestion/jira-webhooks.md`](../../docs/ingestion/jira-webhooks.md#the-api-credential) has the fields, which the Python workers read too.
   4. Deletes the webhooks an earlier connection of the same site registered.

**The token and the webhook belong to whoever consents.** If that person loses access to the site, Jira stops sending and the refresh fails. So Settings asks owners to consent as a Jira user kept for integrations. Atlassian allows 5 webhooks per app for each user on a site.

**Disconnect** refreshes the token if it is about to expire, deletes the site's webhooks, then deletes the credential. If Jira refuses, the notice says so: the webhook lapses within 30 days, and until then intake stores its deliveries with no tenant.

**Refresh tokens rotate.** Each refresh returns a new refresh token, and the old one works for only 10 more minutes, so `freshJiraCredential` refreshes under a row lock and commits the new pair before using it. `enrich:jira` and the keep-alive follow the same rule.

It needs Postgres with the ingestion migrations, the 3LO app in `JIRA_OAUTH_CLIENT_ID` and `JIRA_OAUTH_CLIENT_SECRET`, intake's address in `JIRA_WEBHOOK_URL`, and `SOURCE_CREDENTIALS_API_KEYS`. Without them Settings says what is missing.

### Atlassian app settings

In the [Atlassian developer console](https://developer.atlassian.com/console/myapps/), create an **OAuth 2.0 integration**:

1. **Permissions → Jira API:** add `read:jira-work`, `read:jira-user` and `manage:jira-webhook` (classic scopes). `offline_access` is asked for at consent and needs no setting.
2. **Authorization → OAuth 2.0 (3LO):** callback URL `https://pi-cp.tail470a31.ts.net/connect/jira/callback`, which is `BETTER_AUTH_URL` plus the path.
3. **Distribution:** sharing on. Jira delivers a private app's dynamic webhooks only when the app's owner registered them, so without this, only the owner's own consent would work.
4. **Settings:** put the Client ID and Secret in the vault. The deploy writes them into the web app's and intake's Secrets. Intake verifies each delivery's bearer token with the same secret.

For local dev, a second app with callback `http://localhost:3000/connect/jira/callback` keeps the shared app's settings alone. Deliveries need a public `JIRA_WEBHOOK_URL`, e.g. a tunnel to a local intake.

## Connect Slack

An organization's owners and admins connect a Slack workspace in Settings → Connections (EMBER-148, [ADR 0010](../../docs/adr/0010-connecting-sources.md#slack-ember-148)). It is Slack's OAuth v2 install of Ember's one Slack app (`src/lib/slack-connect.ts`):

1. **Connect Slack** (`/connect/slack`) sends the owner to Slack's install screen for the bot scopes in `BOT_SCOPES` (the manifest's, and no user scopes), with a `state` that a signed, 15-minute cookie ties to this browser, user and organization. The owner picks the workspace there.
2. **The callback** (`/connect/slack/callback`) trades Slack's `code` with `oauth.v2.access`. Only someone Slack lets install apps in the workspace gets a code, and the answer names the workspace (`team.id`), which is the proof of ownership. No id the browser sends is trusted. It refuses:
   - an answer without a bot token, or without every scope in `BOT_SCOPES` (the app's configured scopes drifted from the manifest);
   - an organization-wide Enterprise Grid install, which it uninstalls again with the token Slack gave. A workspace inside a Grid organization is a normal install;
   - a workspace another organization holds. Its token there is unchanged.
3. **In one transaction** (`connectWorkspace`, through `connectAccount`), it maps the workspace's `team_id` to the tenant in `installations`, with the workspace name as `label`, and seals the bot token as the workspace's `api` credential: `{bot_token, bot_user_id, scope, app_id, installed_by, enterprise_id}`. `enrich:slack` reads it (`services/slack-ingestion/workspaces.py`), and claims a workspace's rows only once both exist, so it never sees the mapping without the token.
4. **It hands off** the workspace's stored deliveries, now and once more 10 s later, as Connect GitHub does.

Reconnecting a workspace to the same organization (after a Disconnect, or after Slack disconnected it) clears `disconnected_at` and replaces the token. Slack bot tokens don't expire (token rotation is off), so there is nothing to refresh and no keep-alive.

**Disconnect** uninstalls the app from the workspace with `apps.uninstall` and the stored token, then deletes the credential. That stops Slack's events and kills the token. If Slack already removed the app (`invalid_auth`, `account_inactive`, `token_revoked`), that counts as uninstalled. If Slack can't be reached, the notice tells the owner to remove Ember under the workspace's Manage apps: until then Slack keeps sending events, which intake stores with no tenant.

**Slack can end a connection too.** When a workspace removes the app, Slack sends `app_uninstalled` (and `tokens_revoked`), and any call with the dead token is refused. `enrich:slack` then disconnects the workspace, as Disconnect does, and Settings shows it disconnected, to connect again.

It needs Postgres with the ingestion migrations, the Slack app's OAuth client in `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET`, and `SOURCE_CREDENTIALS_API_KEYS`. Without them Settings says what is missing.

### Slack app settings

On the Slack app's page at [api.slack.com/apps](https://api.slack.com/apps) (created from [`services/slack-ingestion/slack-app-manifest.json`](../../services/slack-ingestion/slack-app-manifest.json)):

1. **OAuth & Permissions → Redirect URLs:** `https://pi-cp.tail470a31.ts.net/connect/slack/callback`, which is `BETTER_AUTH_URL` plus the path. It is a browser redirect, so a tailnet address works. The manifest's `redirect_urls` has a placeholder to replace.
2. **Manage Distribution → Activate Public Distribution.** Without it the app installs only into the workspace that created it. Public distribution doesn't list it in the Slack Marketplace, and installs into other workspaces fall under Slack's non-Marketplace limits on `conversations.history` and `conversations.replies` (`docs/ingestion/slack.md`, Rate limits).
3. **Basic Information → App Credentials:** put the Client ID and Client Secret in the vault as `vault_slack_client_id` and `vault_slack_client_secret`. Then run `make web-secret`, commit `infra/k8s/apps/web/auth.sops.yaml`, and restart the web app. The Signing Secret stays where it is, in intake's Secret: one per app, whatever the workspace.

For local dev, a second Slack app with Redirect URL `http://localhost:3000/connect/slack/callback` keeps the shared app's settings alone. If Slack refuses a plain `http://localhost` URL there, use an HTTPS tunnel to the local app and its address.

## Connect GitLab

An organization's owners and admins connect a top-level GitLab group in Settings → Connections (EMBER-149, [ADR 0010](../../docs/adr/0010-connecting-sources.md#gitlab)). GitLab has no install flow that works across GitLab.com and self-managed instances, so the connection is pasted, in two steps on `/connect/gitlab`:

1. **The group.** The owner enters the instance URL (https only), the top-level group's path and a `read_api` token: a group access token on Premium or Ultimate, a personal access token on Free. `connectGroup` in `src/lib/gitlab-connect.ts` then:
   1. Reads the token (`/personal_access_tokens/self`) and refuses one that is revoked, lacks `read_api`, or can write (`api`, `write_repository` and the like): every Ember source credential is read-only.
   2. Reads the group and refuses a subgroup, then checks that the token's user is a member of it (`/groups/:id/members/all/:user`). Reading the group alone proves nothing for a public group. The bot of a group access token is a member of its own group only, so a bot member means a group access token, the strong proof. A personal access token is accepted, and the connection's label names it as the weaker case.
   3. Maps `<instance host>/<group path>`, the `account_id` intake derives from a delivery, to the tenant, with `<host>:<group id>` as `external_id`, so a renamed group can't move tenants either. Runs the hand-off.
   4. Seals the token, the instance URL and what was checked as the group's `api` credential, which `enrich:gitlab` reads. A reconnect replaces it.
2. **The webhooks.** The owner adds a webhook pointing at intake (`GITLAB_WEBHOOK_URL`) to the group (Premium) or to each project (Free), with GitLab's **Generate signing token**, and pastes each one back. `addSigningToken` appends it to the group's `webhook` credential, `{"signing_tokens": [...]}`, which intake verifies deliveries with. GitLab's page can't take a token Ember made, only its API can (with the write scope), and each webhook gets its own, hence a list (at most 200). Settings links each GitLab connection to this step as **Add a webhook**.

Neither token is logged or shown again. Ember doesn't register the webhook itself: that needs the `api` scope. **Disconnect** deletes both credentials. GitLab's webhook stays until the owner removes it; intake answers it 401, and GitLab turns it off after 40 failures.

The web server calls the instance URL the owner typed, so only https is accepted and redirects aren't followed: the token goes to that address and nowhere else. Only owners and admins can reach the form.

It needs Postgres with the ingestion migrations, intake's address in `GITLAB_WEBHOOK_URL`, and both key rings. Without them Settings says what is missing.

## Settings

| Variable | Required | Meaning |
| --- | --- | --- |
| `BETTER_AUTH_SECRET` | yes | Signs session cookies. Generate with `openssl rand -hex 32`. Never commit it. |
| `BETTER_AUTH_URL` | yes, outside local dev | The address users open, e.g. `http://localhost:3000`. Used for cookie and origin checks. |
| `DATABASE_URL` | no | See above. The container defaults to `file:/data/ember.db`. |
| `EMBER_ADMIN_EMAILS` | no | Comma-separated emails of Ember admins, who can [link an organization to an existing tenant](#linking-a-tenant). Unset means nobody can. |
| `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` | no | The `ember-ingest` App's OAuth client, for [Connect GitHub](#connect-github). Unset means Settings can't connect GitHub. |
| `GITHUB_APP_SLUG` | no | The App's URL name, `github.com/apps/<slug>`. Defaults to `ember-ingest`. |
| `JIRA_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET` | no | The Atlassian 3LO app's client, for [Connect Jira](#connect-jira). Unset means Settings can't connect Jira. |
| `JIRA_WEBHOOK_URL` | no | Where Jira sends the webhooks Connect Jira registers: intake's public `/webhook/jira`, over https. |
| `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` | no | The Slack app's OAuth client, for [Connect Slack](#connect-slack). Unset means Settings can't connect Slack. |
| `GITLAB_WEBHOOK_URL` | no | The webhook URL [Connect GitLab](#connect-gitlab) tells owners to add: intake's public `/webhook/gitlab`, over https. Unset means Settings can't connect GitLab. |
| `SOURCE_CREDENTIALS_API_KEYS`, `SOURCE_CREDENTIALS_WEBHOOK_KEYS` | no | The key rings that seal [source credentials](#source-credentials): `<key id>:<base64 of 32 bytes>`, comma-separated, the sealing key first. Unset means no source that stores a credential can be connected. |

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

## On the cluster

The cluster runs it at **`https://pi-cp.tail470a31.ts.net`**, reachable from the tailnet only (EMBER-112), against the cluster's Postgres. Any node's name works (`https://hv-worker-1.tail470a31.ts.net`, `https://hv-worker-2.tail470a31.ts.net`), and so does `https://ember-web.tail470a31.ts.net` on devices it's shared with. Point the CLI there to sign in against it: `ember auth login --url https://pi-cp.tail470a31.ts.net`. Flux deploys `infra/k8s/apps/web/`, and `infra/k8s/README.md` (web) covers the image tag, the Secrets and the test accounts (`test-owner@`, `test-admin@` and `test-member@example.com`, in organization Ember Test).

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
  - CLI sign-in, called the way the CLI calls it: a token after approval and `whoami` naming the user; a used, denied or expired code; polling too fast; an unknown `client_id`; a made-up token; logging out with the token; browser sign-in not exposing the session token in a header
  - the `/device` page: showing a code claims nothing, so the user can switch accounts before approving; a code another account claimed can't be decided; deciding after signing out or a second time
- `test/organizations.test.ts` covers organizations, on SQLite and Postgres:
  - creating one: the creator is its owner, the slug is the tenant, and it becomes active
  - slugs that aren't valid tenant ids, taken by another organization, or naming a tenant ingestion holds data for (Postgres with the ingestion migrations applied); the slug can't change
  - invites: shown to the invited email, accepted only by it, members can't invite, revoked and expired invites can't be used, and invites last a week
  - switching between two organizations, and the choice persisting in the session
  - linking a tenant: Ember admins only, one organization per tenant (also enforced by the database)
- `test/connect-github.test.ts` covers Connect GitHub, with GitHub as a fake `fetch`:
  - the signed state and picker cookies: altered, expired, from another browser or another user
  - an installation GitHub lists for the user is accepted, one it doesn't is refused, every page is read, and the user token is revoked
  - the picker offers only installations the user administers, and says when the App can't read organization roles
  - on Postgres with the ingestion migrations applied: connecting hands stored deliveries over and notifies, the second hand-off, refusing a move (also by a reinstall), reconnecting, and what the picker learns
- `test/connect-jira.test.ts` covers Connect Jira, with Atlassian as a fake `fetch`:
  - the app settings, the consent URL's scopes, and the state cookie, which GitHub's cookies can't stand in for
  - the token exchange, only Jira sites offered, projects read page by page, and a webhook Jira won't register
  - the consent waiting for the picker: sealed, and only for the same organization and user until it expires
  - on Postgres with the ingestion migrations applied: connecting registers a webhook for the ticked projects, maps the site and seals the tokens; reconnecting deletes the earlier webhook; a site another tenant holds isn't moved; an expiring token is refreshed once under the row lock; Disconnect deletes the webhook, or says it couldn't
- `test/connections.test.ts` covers Connections and source credentials:
  - every vector in `packages/source-credentials/fixtures/vectors.json` opens and seals to the same bytes as the Python workers
  - a credential copied to another account, source or kind, a changed byte, an unknown key and a bad key ring are refused, without echoing the key
  - on Postgres with the ingestion migrations applied: storing and reading a credential, which needs its connection; Disconnect stops the mapping, deletes the credential and keeps the data; another organization can't disconnect or take the account; reconnecting; listing every source's connections
- `test/workspace.test.ts` covers:
  - reading references and signals out of text
  - the preview data's consistency
  - the exact insights it produces
  - filtering by source
  - the dashboard numbers
  - the graph's fit to the ontology
  - that each insight's action resolves it, and only it

CI runs them against a Postgres service container with the ingestion migrations applied.

## Conventions

- Owns only the UI and the web app's auth. Shared logic that a second consumer needs moves to `packages/` (see `packages/README.md`).
- API calls are same-origin and relative, and nothing loads from a CDN (icons and source marks are inline SVG, and the fonts, Geist and Geist Mono, are files in `src/app/fonts/` served through `next/font/local`), so the future Electron shell can load the app unchanged and offline.
- Styling is plain CSS: design tokens (color for both themes, type and spacing scales, radii, shadows, motion) and shared pieces in `src/app/globals.css`, one CSS module per page or component. Modules use the tokens, not raw values, so both themes stay in step.
- The graph layout uses `d3-force`. Everything else on the page is React and SVG.
