//! `ember`, the Ember command-line interface (epic EMBER-11).

mod auth;
mod db;
mod status;

use std::fmt;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

/// The Ember command-line interface.
#[derive(Parser)]
#[command(name = "ember", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show what ingestion data is in Postgres: records per source and the newest of each
    Status,
    /// Sign in to the Ember web app, and check or end that sign-in
    Auth {
        #[command(subcommand)]
        command: AuthCommand,
    },
}

#[derive(Subcommand)]
enum AuthCommand {
    /// Sign in through the browser
    Login {
        /// The web app's address [env: EMBER_URL] [default: http://localhost:3000]
        #[arg(long)]
        url: Option<String>,
        /// Only print the code and address; don't open a browser
        #[arg(long)]
        no_browser: bool,
    },
    /// Show who you are logged in as
    Status,
    /// Sign out and delete the stored token
    Logout,
}

/// A failure, worded for the person at the terminal.
#[derive(Debug)]
pub enum Error {
    /// Setup the user has to fix, such as DATABASE_URL. Exits 2, like intake and the enrich workers.
    Config(String),
    /// Postgres could not be reached or queried. Exits 1.
    Database(String),
    /// Signing in failed, or the web app could not be reached or did not accept the token. Exits 1.
    Auth(String),
}

impl Error {
    fn exit_code(&self) -> ExitCode {
        match self {
            Error::Config(_) => ExitCode::from(2),
            Error::Database(_) | Error::Auth(_) => ExitCode::from(1),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Config(message) | Error::Database(message) | Error::Auth(message) => {
                f.write_str(message)
            }
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Status => status::run().await,
        Command::Auth { command } => match command {
            AuthCommand::Login { url, no_browser } => auth::login(url, no_browser).await,
            AuthCommand::Status => auth::status().await,
            AuthCommand::Logout => auth::logout().await,
        },
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            error.exit_code()
        }
    }
}
