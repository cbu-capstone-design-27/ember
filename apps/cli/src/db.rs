//! The Postgres connection. Like every Ember worker, it comes from DATABASE_URL and nothing else.

use std::time::Duration;

use sqlx::ConnectOptions;
use sqlx::postgres::{PgConnectOptions, PgConnection};

use crate::Error;

const EXAMPLE: &str = "postgresql://ember:...@localhost:5432/ember?sslmode=disable";

/// How long to wait for Postgres before calling it unreachable. A host that drops packets would
/// otherwise hang for the OS's TCP timeout, which can be minutes.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// Reads DATABASE_URL.
pub fn options_from_env() -> Result<PgConnectOptions, Error> {
    parse(std::env::var("DATABASE_URL").unwrap_or_default().trim())
}

/// Checks that `url` names a Postgres database. Error messages never repeat the URL, because it
/// holds the password.
fn parse(url: &str) -> Result<PgConnectOptions, Error> {
    if url.is_empty() {
        return Err(Error::Config(format!("Set DATABASE_URL, e.g. {EXAMPLE}")));
    }
    // The web app reads the same variable and also takes file: (SQLite) URLs
    if !url.starts_with("postgres://") && !url.starts_with("postgresql://") {
        return Err(Error::Config(format!(
            "DATABASE_URL must be a postgres:// or postgresql:// URL, e.g. {EXAMPLE}"
        )));
    }
    url.parse().map_err(|error| {
        Error::Config(format!("DATABASE_URL is not a valid Postgres URL: {error}"))
    })
}

/// Where `options` points, without the user or password: `host:port/database`.
pub fn describe(options: &PgConnectOptions) -> String {
    let database = options.get_database().unwrap_or("");
    match options.get_socket() {
        Some(socket) => format!("{}/{database}", socket.display()),
        None => format!("{}:{}/{database}", options.get_host(), options.get_port()),
    }
}

/// Opens one connection, or says why it couldn't.
pub async fn connect(options: &PgConnectOptions) -> Result<PgConnection, Error> {
    let unreachable = |why: String| {
        Error::Database(format!(
            "cannot connect to Postgres at {}: {why}",
            describe(options)
        ))
    };
    match tokio::time::timeout(CONNECT_TIMEOUT, options.connect()).await {
        Ok(Ok(connection)) => Ok(connection),
        Ok(Err(error)) => Err(unreachable(reason(&error))),
        Err(_) => Err(unreachable(format!(
            "no answer after {}s",
            CONNECT_TIMEOUT.as_secs()
        ))),
    }
}

/// Why `error` happened. For an error from the server, just its message: sqlx's own wording adds
/// the line of Postgres's C source that raised it.
pub fn reason(error: &sqlx::Error) -> String {
    match error {
        sqlx::Error::Database(db_error) => db_error.message().to_string(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config_error(url: &str) -> String {
        match parse(url) {
            Err(Error::Config(message)) => message,
            other => panic!("expected a config error for {url:?}, got {other:?}"),
        }
    }

    #[test]
    fn takes_both_postgres_schemes() {
        for url in [
            "postgres://ember:pw@db.example:6543/ember",
            "postgresql://ember:pw@db.example:6543/ember?sslmode=require",
        ] {
            assert_eq!(describe(&parse(url).unwrap()), "db.example:6543/ember");
        }
    }

    #[test]
    fn unset_says_what_to_set() {
        assert!(config_error("").starts_with("Set DATABASE_URL, e.g. postgresql://"));
    }

    #[test]
    fn refuses_the_web_apps_sqlite_url() {
        assert!(
            config_error("file:./data/ember.db")
                .contains("must be a postgres:// or postgresql:// URL")
        );
    }

    #[test]
    fn bad_url_does_not_echo_the_password() {
        let message = config_error("postgresql://ember:hunter2@db.example:notaport/ember");
        assert!(
            message.starts_with("DATABASE_URL is not a valid Postgres URL"),
            "{message}"
        );
        assert!(!message.contains("hunter2"), "{message}");
    }

    #[test]
    fn describe_leaves_out_the_credentials() {
        let options = parse("postgresql://ember:hunter2@db.example/ember").unwrap();
        assert_eq!(describe(&options), "db.example:5432/ember");
    }
}
