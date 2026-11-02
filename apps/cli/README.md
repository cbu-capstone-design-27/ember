# apps/cli

`ember`, the Ember command-line interface. Epic EMBER-11; the scaffold and `ember status` are EMBER-82, `ember auth` is EMBER-87, the terminal UI is EMBER-120. Written in Rust ([ADR 0007](../../docs/adr/0007-cli-in-rust.md)).

| Command | What it does |
| --- | --- |
| `ember` | In a terminal, opens the [terminal UI](#terminal-ui). Anywhere else (a pipe, a script), prints the help and exits 2. |
| `ember status` | What ingestion data is in Postgres: for each source, how many `source_records` rows there are and the newest `occurred_at`. |
| `ember auth login` | Sign in to the web app through the browser, and store the token. |
| `ember auth status` | Who you're logged in as, and on which web app. |
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
Logged in to http://localhost:3000 as ada@example.com
Organization: Kestrel Labs (kestrel-labs)
$ ember auth logout
Logged out of http://localhost:3000
```

1. `login` asks the web app for a code, and opens your browser at the page for it. If no browser opens (or you pass `--no-browser`), open the printed address yourself and check that the code matches.
2. Sign in or sign up in the browser, then approve the code. `login` checks for your answer every few seconds.
3. It asks the web app who you are and prints your **email**, because accounts have no username.

- `--url <address>` or `EMBER_URL` picks the web app (`--url` wins). It must be an `http://` or `https://` address. The address is stored with the token, so `status` and `logout` talk to the server that issued it.
- Running `login` while logged in goes through the flow again and replaces the token.
- `status` asks the web app whether the token still works. A token expires after 7 days without use, and `status` counts as use. If it has expired or was revoked, `status` says "Not logged in. Run `ember auth login`." and leaves the file for `login` to replace.
- `status` also prints the organization the CLI works in (EMBER-124). The CLI has a session of its own, so switching in the web app's sidebar doesn't change it: it's the first organization you joined, until the CLI can switch. With none, it says where to create or join one.
- `logout` ends the session on the web app, then deletes the file. If the web app can't be reached or no longer accepts the token, it still deletes the file and warns you that the session may still be open there. With nothing stored it says "Not logged in" and exits 0.
- The token is never printed.

### Checking sign-in against the cluster

EMBER-120 checks this flow against the web app on the cluster, which is tailnet-only (EMBER-112). Run it once that deploy lands, and again after changing sign-in on either side:

1. Join the cluster's tailnet, as in [`apps/web/README.md`](../web/README.md#cluster-postgres) step 1.
2. Point the CLI at the deployed web app. Its address is in the EMBER-112 deploy docs (`infra/k8s/README.md`): `export EMBER_URL=<web app address>`.
3. `ember auth login`. The browser opens the web app's `/device` page with the same code the terminal shows. Sign in, approve, and the terminal prints `Successfully authenticated as <your email>`.
4. `ember auth status` prints `Logged in to <web app address> as <your email>`, then your organization. On a fresh cluster you have none until you create one, or an Ember admin adds you to the one linked to `ember-development`.
5. `ember` opens the terminal UI. The Workspace screen shows the same account and organization.
6. `ember auth logout` prints `Logged out of <web app address>`, and `ember auth status` then says `Not logged in`.

If a step fails, the error names the address and the reason (unreachable, HTTP status). The [request reference](../web/README.md#cli-sign-in) has a `curl` version of the same flow, to tell a CLI problem from a web app one.

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
| 1 | Not logged in; sign-in denied, expired or already used; the token was rejected; the web app couldn't be reached or answered something unexpected. Every request gives up after 10 s. |
| 2 | Configuration: the web app address isn't an http(s) URL, or there's no config directory. Also bad arguments. |

## Terminal UI

`ember` with no command, in a terminal, opens a full-screen UI (EMBER-120, built with [ratatui](https://ratatui.rs)). It's a skeleton for now:

- **Status**: each source's record count, newest record and state (live, backfilling, not connected), like `ember status`.
- **Workspace**: who you're signed in as and the organization the CLI works in, as `ember auth status` reports them. Below that, the organization's members and what it ingests.

| Key | Does |
| --- | --- |
| `Tab`, `→`, `l` / `Shift+Tab`, `←`, `h` | Next / previous screen |
| `1`, `2` | Jump to a screen |
| `q`, `Esc`, `Ctrl+C` | Quit |

The sign-in is real: the UI opens straight away and asks the web app in the background, so the corner says "checking sign-in…" for a moment. Everything else is preview data, the made-up Kestrel Labs team from the web app's preview, labelled as such on screen. That data is in `src/tui/data.rs` (`Preview::stub`). When the API decision (EMBER-115) lands, a call to that API replaces it, and the screens stay as they are.

Quitting puts the terminal back as it was, and so does a crash. Exit codes: 0 when you quit, 1 when the terminal can't be used, 2 for plain `ember` without a terminal (it prints the help instead).

## Development

```sh
cd apps/cli
cargo run                                     # build and open the terminal UI
cargo run -- status                           # build and run a command
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
| `src/tui/mod.rs` | The terminal UI: taking over the terminal, the event loop, asking for the sign-in in the background |
| `src/tui/app.rs` | Its state and what each key does |
| `src/tui/data.rs` | What it shows: the sign-in, and the preview data that stands in for the API |
| `src/tui/view.rs` | The tab bar and key hints, and the formatting the screens share |
| `src/tui/status.rs`, `src/tui/workspace.rs` | The two screens. Their tests draw them on ratatui's test backend and read the text back |
| `tests/status.rs` | Runs the built binary, against Postgres for the ignored tests |
| `tests/auth.rs` | Runs the built binary against a fake web app; needs no database |
| `tests/tui.rs` | Plain `ember` without a terminal prints the help instead of opening the UI |
| `rust-toolchain.toml` | The Rust version everyone and CI build with |

## Conventions

- The CLI shares no code with the Python services and the TypeScript web app. Its contracts with them are the Postgres schema in [`docs/ingestion/architecture.md`](../../docs/ingestion/architecture.md) and, from EMBER-87, the web app's HTTP API.
- It reads the database through `DATABASE_URL` only, like every worker ([`AGENTS.md`](../../AGENTS.md)). Queries are written as plain SQL strings, without sqlx's compile-time checked macros, so building the CLI never needs a database.
