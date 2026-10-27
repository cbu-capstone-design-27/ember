# apps/cli

`ember`, the Ember command-line interface. Epic EMBER-11; the scaffold and `ember status` are EMBER-82. Written in Rust ([ADR 0007](../../docs/adr/0007-cli-in-rust.md)).

| Command | What it does |
| --- | --- |
| `ember status` | What ingestion data is in Postgres: for each source, how many `source_records` rows there are and the newest `occurred_at`. |

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
| `DATABASE_URL` | yes | The ingestion Postgres, as `postgres://` or `postgresql://`. The same variable every worker reads. |

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
| `tests/status.rs` | Runs the built binary, against Postgres for the ignored tests |
| `rust-toolchain.toml` | The Rust version everyone and CI build with |

## Conventions

- The CLI shares no code with the Python services and the TypeScript web app. Its contracts with them are the Postgres schema in [`docs/ingestion/architecture.md`](../../docs/ingestion/architecture.md) and, from EMBER-87, the web app's HTTP API.
- It reads the database through `DATABASE_URL` only, like every worker ([`AGENTS.md`](../../AGENTS.md)). Queries are written as plain SQL strings, without sqlx's compile-time checked macros, so building the CLI never needs a database.
