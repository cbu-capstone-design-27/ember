# apps/cli

`ember`, the Ember command-line interface. Epic EMBER-11; the scaffold and `ember status` are EMBER-82, `ember auth` is EMBER-87. Written in Rust ([ADR 0007](../../docs/adr/0007-cli-in-rust.md)).

| Command | What it does |
| --- | --- |
| `ember status` | What ingestion data is in Postgres: for each source, how many `source_records` rows there are and the newest `occurred_at`. |
| `ember auth login` | Sign in to the web app through the browser, and store the token. |
| `ember auth status` | Who you're signed in as, and on which web app. |
| `ember auth logout` | End the sign-in on the web app and delete the stored token. |

## Install

You need Rust. Install it once with [rustup](https://rustup.rs):

```sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Then build and install `ember` from this directory. `rust-toolchain.toml` pins the compiler version, and rustup downloads it the first time:

```sh
cd apps/cli
cargo install --locked --path .
ember --version
```

`cargo install` puts the binary in `~/.cargo/bin`, which rustup adds to your `PATH`. Run the same command again to update.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | for `ember status` | The ingestion Postgres, as `postgres://` or `postgresql://`. The same variable every worker reads. |
| `EMBER_URL` | no | The web app's address for `ember auth login`, an `http://` or `https://` URL. Default `http://localhost:3000`. `--url` wins over it. |
| `EMBER_CONFIG_DIR` | no | Where the credentials file goes. Default: `ember` inside your platform's config directory (below). |

The web app reads `DATABASE_URL` too and also accepts `file:` (SQLite) URLs. `ember` refuses those, because the ingestion data is only in Postgres.

The local Postgres from `infra/docker-compose.yml` (profile `postgres`, migrations applied with `postgres-migrate`):

```sh
export DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable'
```

The cluster's Postgres, from the tailnet. `make postgres-url` reads the password from the cluster, so it needs the kubeconfig ([`infra/k8s/README.md`](../../infra/k8s/README.md), Postgres):

```sh
export DATABASE_URL="$(make -s -C infra/ansible postgres-url)"
```

`sslmode` works as in `psql`. Without it, `ember` uses TLS when the server offers it, which the cluster's does. `require` insists on TLS. `verify-ca` and `verify-full` also check the certificate against `sslrootcert`.

## Usage

```console
$ ember status
SOURCE  RECORDS  NEWEST
github     1482  2026-10-14T21:03:11Z
jira        311  2026-10-14T19:55:02Z
slack         0  -
gitlab      127  2026-10-13T08:41:27Z
```

- The four launch sources are always listed, in this order, with `0` and `-` when they have no records. Any other source that has records comes after them.
- `RECORDS` counts rows, so each stored version of an object counts once.
- `NEWEST` is the latest `occurred_at`, the time the source gave the event (EMBER-63), not when Ember received it. It's shown in UTC, to the second.

| Exit code | Meaning |
| --- | --- |
| 0 | Printed the table |
| 1 | Postgres could not be reached or read, e.g. it's down, the password is wrong, or the migrations were never applied. Connecting gives up after 10 s. |
| 2 | Configuration: `DATABASE_URL` is unset or isn't a Postgres URL. Also bad arguments. |

Errors go to stderr and never include the password.

## Sign in

Accounts are made on the web app only. `ember auth login` signs in with the OAuth device flow (RFC 8628) that the web app serves; the exact requests are in [`apps/web/README.md`](../web/README.md#cli-sign-in).

```console
$ ember auth login
Your code is WDJB-MJHT
Approve it at http://localhost:3000/device
Waiting for you to approve it in the browser...
Successfully authenticated as ada@example.com
$ ember auth status
Signed in to http://localhost:3000 as ada@example.com
$ ember auth logout
Logged out of http://localhost:3000
```

1. `login` asks the web app for a code, and opens your browser at the page for it. If no browser opens (or you pass `--no-browser`), open the printed address yourself and check that the code matches.
2. Sign in or sign up in the browser, then approve the code. `login` checks for your answer every few seconds.
3. It asks the web app who you are and prints your **email**, because accounts have no username.

- `--url <address>` or `EMBER_URL` picks the web app (`--url` wins). It must be an `http://` or `https://` address. The address is stored with the token, so `status` and `logout` talk to the server that issued it.
- Running `login` while signed in goes through the flow again and replaces the token.
- `status` asks the web app whether the token still works. A token expires after 7 days without use, and `status` counts as use. If it has expired or was revoked, `status` says so and leaves the file for you to replace with `login`.
- `logout` ends the session on the web app, then deletes the file. If the web app can't be reached or no longer accepts the token, it still deletes the file and warns you that the session may still be open there. With nothing stored it says "Not signed in" and exits 0.
- The token is never printed.

The credentials file is `credentials.json`, holding the web app's address, the token and your email. The directory is created if it's missing.

| OS | Default location |
| --- | --- |
| Linux | `~/.config/ember/credentials.json` (`$XDG_CONFIG_HOME/ember`) |
| macOS | `~/Library/Application Support/ember/credentials.json` |
| Windows | `%APPDATA%\ember\credentials.json` |

On Linux and macOS the file is created with mode `0600` and the directory with `0700`, so only you can read them. The file is written to a temporary file and renamed, so an interrupted login can't leave half a token. Windows has no such modes; the file inherits your profile folder's access.

| Exit code | Meaning |
| --- | --- |
| 0 | Done, or (`logout`) nothing to do |
| 1 | Not signed in; sign-in denied, expired or already used; the token was rejected; the web app couldn't be reached or answered something unexpected. Every request gives up after 10 s. |
| 2 | Configuration: the web app address isn't an http(s) URL, or there's no config directory. Also bad arguments. |

## Development

```sh
cd apps/cli
cargo run -- status                           # build and run
cargo test                                    # unit tests, and the end-to-end tests that need no database
cargo fmt                                     # format
cargo clippy --all-targets -- -D warnings     # lint
```

CI also runs the end-to-end tests that need a database. These tests apply the repo migrations with dbmate to a fresh database, then run the built `ember` against it. Like the ingestion workers' tests, they need `dbmate` and a Postgres with pgvector that they may create and drop databases on. The compose Postgres works too.

```sh
docker run -d --name pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg18-trixie
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
  cargo test -- --include-ignored
```

They use the databases `ember_test_cli_status` and `ember_test_cli_unmigrated`. Set `DBMATE` if dbmate isn't on your `PATH`.

| Path | Job |
| --- | --- |
| `src/main.rs` | The commands (clap), and how errors become messages and exit codes |
| `src/db.rs` | `DATABASE_URL`, connecting, and wording errors without the password |
| `src/status.rs` | `ember status`: the query and the table |
| `src/auth.rs` | `ember auth`: the device flow, `whoami`, sign-out, and the credentials file |
| `tests/status.rs` | Runs the built binary, against Postgres for the ignored tests |
| `tests/auth.rs` | Runs the built binary against a fake web app; needs no database |
| `rust-toolchain.toml` | The Rust version everyone and CI build with |

## Conventions

- The CLI shares no code with the Python services and the TypeScript web app. Its contracts with them are the Postgres schema in [`docs/ingestion/architecture.md`](../../docs/ingestion/architecture.md) and, from EMBER-87, the web app's HTTP API.
- It reads the database through `DATABASE_URL` only, like every worker ([`AGENTS.md`](../../AGENTS.md)). Queries are written as plain SQL strings, without sqlx's compile-time checked macros, so building the CLI never needs a database.
