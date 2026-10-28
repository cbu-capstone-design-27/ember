# ADR 0007: The CLI is written in Rust

- Status: Proposed
- Date: 2026-10-15
- Jira: EMBER-82

## Context

EMBER-82 starts `apps/cli` and leaves the language to the person building it. It suggests Rust. The language is hard to change once commands exist, so this ADR records the choice.

What the CLI has to do (epic EMBER-11):
- **Install with one command** on developers' machines (macOS, Linux, Windows), including machines with no Python or Node set up.
- **Sign in through the web app** (EMBER-87), then **register projects and configure MCP** in the user's AI tool. It is the single integration path for terminal-only and desktop users, so the Electron app (EMBER-17) uses it too.
- **Report on the system**, starting with `ember status` (EMBER-82), which reads Postgres through `DATABASE_URL` like every worker.

The repo is Python (the ingestion services) and TypeScript (the web app, ADR 0006).

Options considered:
- **Python.** Matches the workers, and could reuse their SQLAlchemy models. But users would need a working Python, or a bundler (PyInstaller, shiv) that is slow to start and awkward to sign. And the workers' code isn't packaged to be imported by anything else.
- **TypeScript on Node.** Matches the web app. Needs Node installed, or a compiled single binary from Bun or Deno, which adds a second JavaScript runtime to the repo.
- **Go.** A single static binary and a small language. A fair alternative, but nobody on the team prefers it.
- **Rust.** A single native binary with no runtime to install, it cross-compiles to every OS the CLI targets, and has mature libraries for everything the epic needs (clap, sqlx, an HTTP client). It's the ticket's suggestion and the owner's choice. It's harder to learn than the others, and new to the team.

## Decision

**`apps/cli` is a Rust crate that builds one binary, `ember`.**

- **Toolchain:** edition 2024. `apps/cli/rust-toolchain.toml` pins the compiler, rustfmt and clippy, so local builds and CI match. Bump it in its own PR.
- **Libraries:**
  - **clap** (derive) for commands and help.
  - **tokio** as the async runtime, single-threaded.
  - **sqlx** for Postgres. Default features are off and its compile-time query macros aren't used, so building never needs a database. TLS is rustls, so the binary links no OpenSSL; `sslmode` behaves as in `psql`.
- **Configuration** follows the workers: Postgres comes from `DATABASE_URL` only.
- **Errors** are one message on stderr, never containing the password. Exit codes: 2 for configuration (as intake and the enrich workers do), 1 for a failure at run time.
- **Tests:** `cargo test` runs unit tests and end-to-end tests of the built binary. Tests that need Postgres are `#[ignore]`d locally. CI runs them with `--include-ignored` against Postgres 18.6 with pgvector, the cluster's version, with the repo migrations applied by dbmate, the same setup the worker tests use.
- **CI:** a `cli` job runs only when `apps/cli`, the migrations, or `ci.yml` change. It checks formatting (`cargo fmt --check`), lints with warnings as errors (`cargo clippy -D warnings`), builds, and tests with `Cargo.lock` enforced. `Cargo.lock` is committed, since this crate is an application.

## Consequences

- Users install one file. Release builds per OS (and an installer script or Homebrew tap) come with the install story in EMBER-11. This ADR doesn't decide that.
- Rust is the repo's third language, and the first with a compile step this slow. CI caches builds between runs.
- The CLI can't import Python or TypeScript code, so `packages/` won't hold code it shares with them. Its contracts are the Postgres schema in `docs/ingestion/architecture.md` and the web app's HTTP API (EMBER-81). A schema change that breaks the `ember status` query fails the CLI's CI job, because that job also runs on migration changes.
- Dependabot watches `apps/cli`'s Cargo dependencies.
