# apps/web

The Ember web app: sign-up, log-in, log-out, and (later) the dashboard and context center. Jira: EMBER-51, epic EMBER-13. Stack decision: [ADR 0005](../../docs/adr/0005-web-app-stack.md).

Next.js (App Router, TypeScript) serves the pages and the auth API from one Node server. Auth is [Better Auth](https://www.better-auth.com/) with email and password. GitHub OAuth (EMBER-12) and organizations/tenants are later additions. The Electron desktop app (EMBER-17) will wrap this same app; it doesn't get a second UI.

## Database

`DATABASE_URL` picks the database. Nothing else changes:

| `DATABASE_URL` | Database |
| --- | --- |
| `file:./data/ember.db` (default) | SQLite, through Node's built-in `node:sqlite` (no native module) |
| `postgres://user:pass@host:5432/db` | Postgres |

The auth tables (`user`, `session`, `account`, `verification`) are created or updated when the server starts (`src/instrumentation.ts`).

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

## Pages

| Path | What it does |
| --- | --- |
| `/` | Signed out: links to log in or sign up. Signed in: the user's email and a Log out button. |
| `/signup` | Name, email, password (at least 8 characters). Signing up also logs in. |
| `/login` | Email and password. |
| `/api/auth/*` | Better Auth's endpoints (`sign-up/email`, `sign-in/email`, `sign-out`, `get-session`, …). |

## Test

```sh
cd apps/web
npm run typecheck
npm test                                            # SQLite
TEST_POSTGRES_URL=postgres://user:pass@localhost:5432/ember npm test   # SQLite and Postgres
```

The tests drive Better Auth's real handler. They cover sign-up (and signing in with it), duplicate email, short password, log-in with the right and wrong password, log-out ending the session, and that passwords are stored hashed. CI runs them against a Postgres service container.

## Conventions

- Owns only UI and the web app's auth. Shared logic that a second consumer needs moves to `packages/` (see `packages/README.md`).
- API calls are same-origin and relative, so the future Electron shell can load the app unchanged.
