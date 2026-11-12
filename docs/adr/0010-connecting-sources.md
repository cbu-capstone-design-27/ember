# ADR 0010: Connecting a source from the web app

- Status: Accepted
- Date: 2026-11-04
- Jira: EMBER-96

## Context

Intake stores each delivery with the tenant that `installations` maps its account to, or `null` when there is none ([ADR 0004](0004-ingestion-architecture.md), [`architecture.md`](../ingestion/architecture.md#the-parent-listener-intake)). Enrich skips null-tenant rows. Today an operator makes that mapping by hand with `make map-tenant` (EMBER-95, `infra/ansible/playbooks/map-tenant.sql`), and the API credentials each enrich worker uses are one Secret per source, written into the manifests. Installation `164866568` was mapped to `ember-development` that way on 2026-10-21. `171593025` is still unmapped.

`architecture.md` names `ember-admin map` as the replacement, but nothing says how a *customer* connects a source. Since this was written down as an open question, the web app has gained the pieces a flow needs:

- **Organizations** (EMBER-124). Each Better Auth organization owns a tenant. Its slug becomes the tenant, and the slug can't change.
- **Postgres access.** The web app runs on the cluster (EMBER-112) with the same `DATABASE_URL` as ingestion. It already reads `installations`, `raw_events` and `source_records` to keep a new organization from taking a tenant that holds data.

EMBER-113 asks for owners to connect GitHub, Jira, Slack and GitLab from Settings, for each connection to be stored per tenant, for no connector credentials in manifests, and for removing a connection to stop its ingestion. EMBER-130 (GitHub) and EMBER-131 (Jira) are its first two subtasks. This ADR is the design they follow.

## Decision

### Tenants

- **A tenant is an organization's `tenant` column.** It starts as the slug (`[a-z0-9][a-z0-9-]*`), is unique, and never changes after creation, because it names every `{tenant}_{source}` subgraph. This is already built (EMBER-124).
- **Anyone signed in can create an organization.** A new slug is refused if another organization or stored ingestion data already holds that tenant. Only an Ember admin can link an organization to a tenant that already holds data (`linkTenant`).
- **Only an organization's owners and admins can connect or disconnect a source.** Members see what is connected. This is the same role check as inviting.
- **One tenant can hold many accounts per source**, for example two GitHub installations or two Slack workspaces. They share the tenant's `{tenant}_{source}` subgraph.
- **One account maps to at most one tenant**, as `installations`' primary key `(source, account_id)` already enforces.

### The connection record

`installations` stays the one place a mapping lives, and intake keeps reading it as it does now. It gains, in a new migration:

- **`disconnected_at`**: null while connected. Intake maps only connected accounts. Deliveries from a disconnected account are stored with `tenant = null`, as an unmapped account's are.
- **`label`**: what Settings shows: the GitHub account login, Slack workspace name, Jira site host or GitLab group path.
- **`external_id`**: the source's stable id for the account behind the connection, where `account_id` is not already that. Only GitHub needs it: an installation id changes on reinstall, the GitHub account id (`installation.account.id`) does not.
- **`connected_by`**: the Better Auth user id that connected it. Null for rows from `make map-tenant`.

**The web app writes `installations`.** It holds the user's session and runs the OAuth exchange, so it is the component that knows who is connecting what. An enrich worker updates only `disconnected_at`, when the source itself reports an uninstall (below).

**Connecting hands stored deliveries to enrich, as `map-tenant.sql` does.** After the `installations` row commits, the web app gives this tenant to that account's `raw_events` rows that have `tenant is null`, then calls `pg_notify('raw_events', source)`. It runs this hand-off twice: once right away, and once at least 10 s later. A delivery intake was storing while the mapping committed can still land with no tenant. Intake gives up on a commit after 8 s, so the second pass catches it.

The web app uses Secret `postgres-app` for now, as every component does. When components get their own Postgres roles ([`architecture.md`, Per-source auth](../ingestion/architecture.md#per-source-auth)), the web role can insert and update `installations` and `source_credentials`, update `raw_events.tenant`, and nothing else.

### Credentials

Per-tenant API credentials go in Postgres, encrypted by the application, in a new `source_credentials` table keyed by `(source, account_id, kind)`:

- **`kind` is `api` or `webhook`.** `api` is what enrich calls the source with. `webhook` is what intake verifies a delivery with, needed only where it is per account (GitLab).
- **Encryption is AES-256-GCM, with two keys:** one for `api` rows, given to the web app and the enrich workers, and one for `webhook` rows, given to the web app and intake. This keeps ADR 0004's rule that the verifying credential and the calling credential never sit in the same pod. The keys are SOPS Secrets (`infra/docs/flux.md`). Each row records its key id, so a key can be rotated by re-encrypting the rows.
- **The web app writes credentials and never shows them again.** Enrich reads `api` rows by `(source, account_id)` in place of today's `SLACK_BOT_TOKEN`, `JIRA_API_TOKEN` and `GITLAB_API_TOKEN` environment variables. Those variables stay as a fallback for the accounts already running on them until each is reconnected.
- **GitHub stores none.** The App's private key already mints an installation token for any installation.

Two alternatives were rejected:

- **Writing Kubernetes Secrets from the web app.** It needs RBAC to create Secrets, and a pod sees a new Secret only after a restart.
- **A separate secret store (Vault, Infisical).** It is one more stateful service on a small cluster for what one table and two keys cover.

### Connecting each source

The pattern is the same everywhere. The **source proves the account belongs to the person connecting it**, the web app records the mapping and any credential, and the hand-off above runs.

| Source | `account_id` (as intake derives it) | How ownership is proven | Credential stored | Webhook set up by |
|---|---|---|---|---|
| GitHub | `installation.id` | User token from installing: the installation is in `GET /user/installations` | None | The App (automatic) |
| Slack | `team_id` | Slack's OAuth v2 install consent | Bot token (`api`) | The app (automatic) |
| Jira | Site host, e.g. `acme.atlassian.net` | Atlassian OAuth 2.0 (3LO) consent, then the site in accessible-resources | Access and refresh token (`api`) | Ember, as a dynamic webhook |
| GitLab | `<instance>/<top-level group>` | The pasted token can read that group | Access token (`api`), signing token (`webhook`) | The owner, by hand |

#### GitHub (EMBER-130)

1. **Turn on "Request user authorization (OAuth) during installation" on `ember-ingest`.** GitHub then redirects to the App's **Callback URL** after an install, with a `code`, `installation_id` and `setup_action`. The Setup URL field is disabled. The Callback URL is the web app's `/connect/github/callback`. The App's client ID and secret go in a SOPS Secret for the web app.
2. **Connect GitHub** sends the owner to `https://github.com/apps/ember-ingest/installations/new?state=<state>`. `<state>` is signed and short-lived, names the organization, and is bound to the browser session. GitHub returns `state` on the callback only when the install started from this link. An install started on GitHub's own page arrives without it and goes to the *existing installation* path below.
3. **The callback exchanges `code` for a user access token**, lists `GET /user/installations`, and accepts `installation_id` only if it is in that list. It then discards the token. GitHub warns that `installation_id` on the redirect can be spoofed, and this check is the defence.
4. **It writes the `installations` row**, with `label` and `external_id` from the installation's account, and runs the hand-off.

The other cases:

- **`setup_action=request`.** An organization member asked an owner to approve the install. There is no installation yet. Settings shows the request as pending. When the owner approves, `installation:created` arrives unmapped.
- **Connect an existing installation.** The person runs GitHub's plain OAuth web flow (same Callback URL, our `state`) and picks from their `GET /user/installations` the ones not yet mapped. This covers an install approved later, one started on GitHub's page, and `171593025`. Membership alone lists an installation, so this path also requires the person to be an admin of the installation's account: the account itself for a user account, an organization owner otherwise. Checking organization role needs the App's organization **Members (read)** permission. Confirm that in EMBER-130.
- **`installation:deleted` and `installation:suspend`.** `enrich:github` sets `disconnected_at`, as `github-webhooks.md` already plans. Stored data stays (EMBER-53). `unsuspend` clears it.
- **Reinstall.** A new install gets a new installation id, so it is a new connection through the same flow. If its `external_id` was connected to a different tenant before, it is refused (see [Moving](#disconnecting-reinstalling-and-moving)).

#### Slack

The Slack connector already plans for this (`slack.md`, Install and multi-workspace):

- **Connect Slack** runs Slack's OAuth v2 install with a signed `state`, and `oauth.v2.access` returns `team.id`, the bot token and the bot user id. Slack's consent screen is the proof of ownership. No separate check is needed.
- **The bot token is stored as an `api` credential.** The signing secret stays one per app, in intake's Secret.
- **`app_uninstalled`, and `tokens_revoked` naming our bot user,** make `enrich:slack` set `disconnected_at`, as `slack-webhooks.md` specifies.
- **Reconnecting** the same workspace to the same tenant clears `disconnected_at` and replaces the token.
- **Enterprise Grid** organization-wide installs are out of scope.

#### Jira (EMBER-131)

Ember moves from a site admin webhook to an Atlassian OAuth 2.0 (3LO) app:

- **Connect Jira** runs 3LO consent. The web app calls accessible-resources, the owner picks a site, and `account_id` is that site's host, as intake derives it today. The site's `cloudId` is kept with the credential, because API calls go through `api.atlassian.com/ex/jira/{cloudId}`.
- **Consent is the proof of ownership.** The token can see only sites the user has access to.
- **The web app registers a dynamic webhook** on the site with the user's token, filtered by JQL to the projects the owner chose. Atlassian signs these deliveries with a bearer JWT made with the app's client secret, so intake's Jira adapter verifies that instead of `X-Hub-Signature`.
- **Dynamic webhooks expire 30 days** after they are created or last refreshed. `scheduler:jira` extends them well before then. Atlassian allows 5 webhooks per app per user on a site.
- **The webhook belongs to the user who connected it.** If that user loses access, Jira stops sending and the token refresh fails. Enrich then marks the connection disconnected, and Settings asks for a reconnect.
- **The access and refresh tokens are the `api` credential.** Enrich refreshes them and writes the new refresh token back, because Atlassian rotates it.
- **The 3LO app falls under Atlassian's points-based quotas** instead of the API-token burst limits (`jira.md`). `rate_limits` keeps one bucket per site.
- **Confirm in EMBER-131:** whether these deliveries still carry the site host in `self` URLs, or only in the JWT's claims. Intake's `account_id` depends on it.

**As built (EMBER-131, 2026-11-04):**

- **The owner picks a site and its projects** after consent (`/connect/jira/pick`). The JQL is `project in (<ids>)`, by id because a key changes on rename. One webhook per site carries the issue, comment and issue property events. Dynamic webhooks aren't offered worklog, attachment or issue link events, so those come from reconcile (`jira-webhooks.md`).
- **Settings asks owners to consent as a Jira user kept for integrations,** because the token and webhook are that user's. Atlassian service accounts were considered: they need an organization admin, a pasted client-credentials secret and a webhook registered by hand. They stay a possible second path.
- **Between the callback and the picker, the tokens wait server side.** They are sealed with the `api` key ring in Better Auth's `verification` table for 15 minutes, so they never reach the browser.
- **The `api` credential holds** the token pair, the cloud id, the webhook ids and the JQL (`jira-webhooks.md`, The `api` credential). Whoever refreshes it locks the row and commits the rotated pair before using it.
- **Disconnect deletes the webhooks** with the stored token before deleting the credential. If Jira refuses, the webhook lapses within 30 days.
- **`scheduler:jira` is a daily Kubernetes CronJob** in the jira-ingestion image (`keep_alive.py`), not part of EMBER-128's sweeper. It refreshes each connected site's token and extends its webhooks. When Atlassian refuses the refresh token (`invalid_grant`), it disconnects the site, as Disconnect does but without deleting the webhooks, since nothing can any more.
- **Enrich reads Jira only with Connect Jira's tokens, and a site's token only for the rows of the tenant that connected it.** The single API-token site enrich used before (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`) is gone, with no fallback: a row whose tenant hasn't connected its site fails and waits. `make map-tenant` still assigns Jira rows a tenant, but enrich can't fetch for them until that tenant connects the site.
- **The app's client secret sits in intake and in enrich.** Intake verifies deliveries with it, and enrich and the keep-alive need it to refresh tokens. That bends ADR 0004's rule that the verifying and the calling credential never share a pod: Atlassian uses one secret for both. Enrich could forge a delivery, but intake still holds no token to call Jira with.
- **Dynamic deliveries carry the site's host in `self` URLs**, as admin ones do, so intake reads the site the same way (first live delivery, 2026-11-05). The token's claims are `context`, `exp`, `iat`, `iss`, `jti` and `sub`. Intake checks only the signature and `exp`; binding a delivery to its site through `context` is a possible hardening.

#### GitLab

GitLab has no install flow that works across GitLab.com and self-managed instances without registering an OAuth app on each instance, so its connection is pasted:

1. **The owner enters the instance URL, the top-level group, and an access token with `read_api`.** That is a Group Access Token on Premium or Ultimate, or a Personal Access Token on Free (`gitlab.md`, Auth).
2. **The web app calls `GET /groups/:id` with that token.** Being able to read the group is the proof. A Group Access Token can be created only by the group's Owners, so it is strong proof. A Personal Access Token proves only that the person can read the group. That is accepted and named as a weaker case.
3. ~~**Ember generates a `whsec_` signing token,** stores it as the `webhook` credential, and shows the webhook URL and token once. The owner adds the webhook to the group (Premium) or to each project (Free).~~ **Amended 2026-11-10, EMBER-149:** GitLab's webhook page can't take a signing token from elsewhere. It offers only **Generate signing token**, and only its API can set one (`signing_token`), with the `api` (write) scope. Each webhook also gets its own, so on Free a group has one per project. So Ember shows the webhook URL, the owner adds the webhook with a generated signing token, and pastes that token back. The `webhook` credential holds every webhook's token: `{"signing_tokens": [...]}`.
4. **Intake looks up the signing tokens** by the `account_id` it derives from the body, before trusting the body, and accepts a delivery signed with any of them. An unknown account is answered 401. Until a group's first token is pasted, the shared `GITLAB_WEBHOOK_SIGNING_TOKEN` still verifies it, for the webhook set up by hand before this flow; it goes once that group is connected.
5. **Auto-registering the webhook was rejected.** It needs a token with the `api` (write) scope, and every other Ember source credential is read-only.

GitLab sends no uninstall event. A GitLab connection ends only through **Disconnect** (below).

#### Teams

Out of scope here. Its account is the Microsoft 365 `tenantId`, through Entra admin consent. It follows the same pattern when the Teams connector is built.

### Disconnecting, reinstalling and moving

- **Disconnect, from Settings,** stops ingestion, as EMBER-113 asks:
  1. Set `disconnected_at`.
  2. Delete the account's `source_credentials`.
  3. Remove the source-side hook where Ember can: Slack `apps.uninstall`, Jira delete the dynamic webhook. GitLab's hook stays until the owner removes it, and intake answers it 401 once its signing token is gone. GitLab disables a hook after 40 failures in a row.
  4. **GitHub (amended 2026-11-04, EMBER-147):** Settings links the owner to GitHub to uninstall `ember-ingest`. Ember doesn't call `DELETE /app/installations/{id}`: that needs an App JWT, so the web app would hold the App's private key, which mints a token for every installation. Only `enrich:github` holds it. Until the owner uninstalls, GitHub keeps sending deliveries. Intake stores them with no tenant, and retention deletes them after 30 days. The uninstall's `installation:deleted` then reaches `enrich:github` (EMBER-150).
- **Disconnecting keeps the tenant's stored data** (EMBER-53). Deleting it is a separate, explicit action: a per-source purge (`ember-admin purge` scoped to tenant and source).
- **Reconnecting the same account to the same tenant** clears `disconnected_at`, and reconcile carries on from its cursor.
- **Moving an account to another tenant is refused,** in the web app as in `map-tenant.sql`, because it would split the account's records across two subgraphs. That includes a disconnected account, and a GitHub reinstall whose `external_id` another tenant held. To move one, its records are first purged from the old tenant. Then it can be connected anew.

### Replay and backfill on connect

- **Connecting hands over every delivery stored for the account while it was unmapped.** Unmapped rows never complete, so today's retention (`completed_at` older than 30 days) never deletes them. Retention will also delete `tenant is null` rows older than 30 days by `received_at`, so an unclaimed account does not grow without limit. What a connect replays is therefore at most the last 30 days.
- **History before that comes from reconcile,** not from stored deliveries. A first connect of an account enqueues that source's reconcile from the beginning, as the onboarding backfill. Until the job queue and `Enricher.reconcile` land, an operator runs the source's `pull_test.py` backfill.
- **Overlap is harmless.** A replayed delivery and a reconcile copy of the same object fold into one `source_records` row on `(source, tenant, object_id, version)`. Enrich applies changes by `occurred_at`, so arrival order does not matter.

**As built (EMBER-167, 2026-11-12):** with no job queue yet, `enrich:github` and `enrich:jira` run the backfill themselves. At start and on every wake, each looks for connected accounts whose `reconcile:<account_id>` cursor isn't `done` and reads their issues, pull requests and comments into `raw_events` with `origin = 'reconcile'`. Progress is in `cursors`, so a restart or a reconnect carries on, and a disconnect stops it. No schema change. Details: [`github-webhooks.md`](../ingestion/github-webhooks.md#reconcile-on-connect-ember-167) and [`jira-webhooks.md`](../ingestion/jira-webhooks.md#reconcile-on-connect-ember-167). Slack and GitLab still use their `pull_test.py` backfills.

### `make map-tenant`

`make map-tenant` stays as the operator override: for accounts connected before this flow (installation `164866568`), and for an Ember admin connecting an account on a customer's behalf. It follows the same rules as the web app:

- It refuses a move.
- It clears `disconnected_at` only for the same tenant.
- It runs the hand-off.

It does not store credentials. `ember-admin map` in `architecture.md` becomes this same operation, once `ember-admin` exists.

## Consequences

- **Owners connect sources themselves,** and a new tenant's deliveries reach enrich with no operator step.
- **Every connection is verified by the source**, never by an id the browser sends.
- **Removing a connection stops its ingestion** without deleting data. Deleting data is a separate, deliberate step.
- **Credentials leave the manifests.** The cost is an encryption key per credential kind to manage and rotate, and a table whose rows are only as safe as those keys.
- **Intake changes for two sources:**
  - Jira verifies a bearer JWT with the 3LO app's client secret.
  - GitLab verifies with a per-account signing token read from Postgres. It is the only adapter that reads Postgres before it stores.
- **Jira gains a recurring job** to keep its webhooks alive, and its rate limiting moves to Atlassian's points quotas.
- **GitHub needs App settings changes:**
  - Request user authorization during installation, which disables the Setup URL.
  - A Callback URL on the web app's address.
  - Organization Members (read).
  - Changing an App's permissions asks every installation to accept again.
- **`architecture.md` and a new dbmate migration** carry the schema changes: `installations`' new columns, `source_credentials`, and the retention rule for unmapped rows. ADR 0004's `installations` table is unchanged in meaning.

## Follow-up tickets

Under EMBER-113, next to EMBER-130 (GitHub) and EMBER-131 (Jira):

- **EMBER-147, schema:**
  - `installations` columns, `source_credentials`, the retention rule for unmapped rows.
  - The connection list in Settings.
  - Disconnect.
- **EMBER-148, connect Slack:**
  - OAuth v2 install.
  - `enrich:slack` reads the bot token from `source_credentials`.
  - `app_uninstalled` and `tokens_revoked`.
- **EMBER-149, connect GitLab:**
  - Pasted token and the webhook signing token.
  - Intake's per-account lookup.
  - `enrich:gitlab` reads its token from `source_credentials`.
- **EMBER-150, `enrich:github` handles `installation` events:**
  - `deleted` and `suspend` set `disconnected_at`, and `unsuspend` clears it.
  - `created` enqueues reconcile once it exists.
- **EMBER-151, `make map-tenant` follows the rules above.**

Under EMBER-12:

- **EMBER-152, a Postgres role per component, starting with the web app.**

## Sources

Checked 2026-11-04.

- [Registering a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app): with "Request user authorization (OAuth) during installation", "Users will instead be redirected to the Callback URL", and the Setup URL can't be entered.
- [About the setup URL](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url): "Bad actors can hit this URL with a spoofed `installation_id`". Generate a user access token and check the installation is associated with that user.
- [List app installations accessible to the user access token](https://docs.github.com/en/rest/apps/installations#list-app-installations-accessible-to-the-user-access-token): installations the user can access, including through organization membership.
- [Jira Cloud webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/): for OAuth 2.0 apps:
  - Expiry 30 days from creation or refresh.
  - Each refresh extends it 30 days.
  - Bearer authentication signed with the app's client secret.
  - 5 webhooks per app per user on a tenant.
  - The supported JQL clauses.
  - `matchedWebhookIds`.
- The `ember-ingest` install `state` round trip, and its absence when the install starts on GitHub's page, come from GitHub's "Preserving an application state during installation" behaviour as reported by integrators. Confirm on the first install in EMBER-130.
