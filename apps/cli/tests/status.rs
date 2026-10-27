//! `ember status` end to end: the built binary, its output and its exit codes.
//!
//! The database tests are `#[ignore]`d so `cargo test` works without Postgres. Like the ingestion
//! workers' tests, they need dbmate and a Postgres server they may create and drop databases on:
//!
//!     PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
//!         cargo test -- --include-ignored
//!
//! They use databases ember_test_cli_status and ember_test_cli_unmigrated.

use std::process::{Command, Output};

use sqlx::{Connection, PgConnection};

const MIGRATIONS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../infra/k8s/apps/postgres/migrations"
);

/// Runs `ember status` with DATABASE_URL set to `database_url`, or unset.
fn ember_status(database_url: Option<&str>) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ember"));
    command.arg("status").env_remove("DATABASE_URL");
    if let Some(url) = database_url {
        command.env("DATABASE_URL", url);
    }
    command.output().expect("run ember")
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

/// PGTEST_URL with its database swapped for `name`.
fn database_url(name: &str) -> String {
    let server = std::env::var("PGTEST_URL")
        .expect("set PGTEST_URL to a Postgres server the test may create databases on");
    let (path, query) = server
        .split_once('?')
        .map_or((server.as_str(), String::new()), |(p, q)| {
            (p, format!("?{q}"))
        });
    let (base, _database) = path
        .rsplit_once('/')
        .expect("PGTEST_URL ends in /<database>");
    format!("{base}/{name}{query}")
}

/// Drops and recreates database `name`, then applies the repo migrations to it if `migrate`.
async fn fresh_database(name: &str, migrate: bool) -> String {
    let mut server = PgConnection::connect(&std::env::var("PGTEST_URL").unwrap())
        .await
        .unwrap();
    // `name` is one of this file's constants, and a database name can't be a bind parameter
    for sql in [
        format!("drop database if exists {name} with (force)"),
        format!("create database {name}"),
    ] {
        sqlx::raw_sql(sqlx::AssertSqlSafe(sql))
            .execute(&mut server)
            .await
            .unwrap();
    }
    server.close().await.unwrap();

    let url = database_url(name);
    if migrate {
        let dbmate = std::env::var("DBMATE").unwrap_or_else(|_| "dbmate".into());
        let output = Command::new(&dbmate)
            .args([
                "--url",
                &url,
                "--migrations-dir",
                MIGRATIONS,
                "--no-dump-schema",
                "up",
            ])
            .output()
            .unwrap_or_else(|error| panic!("run {dbmate}: {error}"));
        assert!(
            output.status.success(),
            "dbmate up failed:\n{}{}",
            stdout(&output),
            stderr(&output)
        );
    }
    url
}

#[tokio::test]
#[ignore = "needs PGTEST_URL and dbmate: cargo test -- --include-ignored"]
async fn counts_records_per_source() {
    let url = fresh_database("ember_test_cli_status", true).await;

    let output = ember_status(Some(&url));
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(
        stdout(&output),
        "SOURCE  RECORDS  NEWEST\n\
         github        0  -\n\
         jira          0  -\n\
         slack         0  -\n\
         gitlab        0  -\n"
    );

    let mut db = PgConnection::connect(&url).await.unwrap();
    // Two tenants for github; two versions of one GitLab MR, the newest given in +02:00 with
    // milliseconds; and teams, which is not a launch source, so it is listed last.
    sqlx::raw_sql(
        "insert into source_records (source, tenant, kind, object_id, version, occurred_at, record) values
           ('github', 'acme', 'pull_request', 'acme/api#1', 'v1', '2026-10-01T09:30:00Z', '{}'),
           ('github', 'acme', 'pull_request', 'acme/api#2', 'v1', '2026-10-02T10:00:00Z', '{}'),
           ('github', 'beta', 'issue',        'beta/web#9', 'v1', '2026-09-01T00:00:00Z', '{}'),
           ('jira',   'acme', 'issue',        'EMBER-1',    'v1', '2026-09-30T08:00:00Z', '{}'),
           ('gitlab', 'acme', 'merge_request', '42!7',      'v1', '2026-10-03T09:00:00Z', '{}'),
           ('gitlab', 'acme', 'merge_request', '42!7',      'v2', '2026-10-03T11:15:42.123+02:00', '{}'),
           ('gitlab', 'acme', 'issue',         '42#3',      'v1', '2026-10-01T00:00:00Z', '{}'),
           ('teams',  'acme', 'message',       'chat/1',    'v1', '2026-10-04T12:00:00Z', '{}')",
    )
    .execute(&mut db)
    .await
    .unwrap();
    db.close().await.unwrap();

    let output = ember_status(Some(&url));
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(
        stdout(&output),
        "SOURCE  RECORDS  NEWEST\n\
         github        3  2026-10-02T10:00:00Z\n\
         jira          1  2026-09-30T08:00:00Z\n\
         slack         0  -\n\
         gitlab        3  2026-10-03T09:15:42Z\n\
         teams         1  2026-10-04T12:00:00Z\n"
    );
}

#[tokio::test]
#[ignore = "needs PGTEST_URL and dbmate: cargo test -- --include-ignored"]
async fn says_when_the_migrations_are_missing() {
    let url = fresh_database("ember_test_cli_unmigrated", false).await;

    let output = ember_status(Some(&url));
    assert_eq!(output.status.code(), Some(1));
    assert!(stdout(&output).is_empty());
    assert!(
        stderr(&output).contains("/ember_test_cli_unmigrated has no source_records table"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn unset_database_url_exits_2() {
    let output = ember_status(None);
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        stderr(&output),
        "error: Set DATABASE_URL, e.g. postgresql://ember:...@localhost:5432/ember?sslmode=disable\n"
    );
}

#[test]
fn unreachable_database_exits_1_without_showing_the_password() {
    // Nothing listens on port 1
    let output = ember_status(Some(
        "postgresql://ember:hunter2@127.0.0.1:1/ember?sslmode=disable",
    ));
    assert_eq!(output.status.code(), Some(1));
    assert!(stdout(&output).is_empty());
    let message = stderr(&output);
    assert!(
        message.starts_with("error: cannot connect to Postgres at 127.0.0.1:1/ember: "),
        "{message}"
    );
    assert!(!message.contains("hunter2"), "{message}");
}
