# ADR 0005: Web app stack

- Status: Proposed
- Date: 2026-10-06
- Jira: EMBER-51

## Context

EMBER-51 scaffolds the Ember web app: sign-up, log-in and log-out, run locally in a container on SQLite, with the database chosen by an environment variable so Postgres can replace SQLite later.

The ticket says the app is "built with Electron Framework". The surrounding plan puts the pieces in this order:

- **EMBER-13** (Web Dashboard & Onboarding, floor): Next.js. It's the primary onboarding surface: sign-in, connecting sources, the CLI install flow.
- **EMBER-14** (Context Center, floor): built once in the web app and **embedded** in the desktop app, not duplicated.
- **EMBER-17** (Desktop App, drop-first): Electron. It **embeds the context center from the web dashboard** and uses the same CLI wiring.
- **EMBER-12** (Auth & Multi-tenancy, floor): GitHub OAuth sign-in, and org and tenant isolation.
- **The proposal** lists the Electron desktop app under "Additional features", the ones dropped first.

An Electron app is a desktop window and can't "run in a container", which EMBER-51 also requires. The two requirements fit together if the web app is built first and Electron wraps it.

Other constraints:
- **Postgres:** ADR 0004 puts Postgres on the cluster, which is where `DATABASE_URL` will point.
- **Containers:** EMBER-42 builds every `apps/<name>/Dockerfile` for `linux/amd64` and `linux/arm64`, so nothing in the image can be tied to one CPU.
- **Tenants:** ADR 0004 and the ontology name tenants with `[a-z0-9][a-z0-9-]*`.

## Decision

**Frontend and backend: Next.js (App Router, TypeScript), one app.** Pages and the auth API run in the same Node server (`apps/web`), so there is one container and one origin. There's no separate auth service to deploy, secure or keep in sync.

**Auth: Better Auth**, email and password first.
- Passwords are hashed (scrypt). Sessions are database-backed, carried in an `HttpOnly`, `SameSite=Lax` cookie signed with `BETTER_AUTH_SECRET`.
- Signing up also signs in. The minimum password length is 8.
- **GitHub OAuth (EMBER-12)** is added later as a `socialProviders.github` entry, and existing accounts keep working. **Organizations and tenants** come from Better Auth's organization plugin. An organization's slug follows the tenant naming rule, so it can be the tenant id in ADR 0004's `installations` table.
- We chose it over Auth.js because Auth.js discourages its password provider and has no sign-up flow of its own. Writing auth by hand was rejected: sessions, CSRF and password storage are easy to get wrong.

**Database: `DATABASE_URL` picks it, nothing else changes.**
- `file:<path>` → SQLite through **Node's built-in `node:sqlite`**. That means no native module, no install script, and the same image on amd64 and arm64.
- `postgres://…` → Postgres through `pg`.
- Better Auth runs its migrations on server start (`src/instrumentation.ts`) against whichever database is set, so a new Postgres needs no manual step.

**Container:** a multi-stage `node:24-slim` image running Next.js's standalone server as `nobody`. SQLite lives in `/data` (a volume), and the compose profile is `web`.

**Electron later, as a shell.** The desktop app (EMBER-17) is a thin Electron wrapper that loads this same web app: the hosted URL, or a bundled local server for self-hosting. It doesn't get a second UI. To keep that cheap, the web app:
- uses only same-origin, relative API calls
- reads its public address from `BETTER_AUTH_URL`
- doesn't rely on browser features Electron lacks

When the shell lands, it adds its own origin to Better Auth's `trustedOrigins`.

## Consequences

- One codebase serves the browser, and later the desktop, satisfying EMBER-14's "built once" rule.
- The web app is the first TypeScript service in a Python-heavy repo. CI gains a Node job (type check, build, auth tests on SQLite and on Postgres).
- `node:sqlite` is marked experimental in Node 24 and prints a warning once. It's only the local and self-hosted default; hosted runs use Postgres.
- Better Auth owns four tables (`user`, `session`, `account`, `verification`). On the cluster Postgres they belong to the web app's own role, which ADR 0004's per-component roles allow.
- Switching to GitHub-only sign-in later (EMBER-12) is a configuration change, not a rewrite. Email and password can stay for self-hosted installs without GitHub.
