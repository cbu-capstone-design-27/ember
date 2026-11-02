//! `ember`, the Ember command-line interface (epic EMBER-11).

mod auth;
mod db;
mod status;
mod tui;

use std::fmt;
use std::io::IsTerminal;
use std::process::ExitCode;

use clap::{CommandFactory, Parser, Subcommand};

/// The Ember command-line interface.
#[derive(Parser)]
#[command(
    name = "ember",
    version,
    about,
    after_help = "Run `ember` with no command in a terminal to open the terminal UI."
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
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
    /// The terminal UI could not take over the terminal. Exits 1.
    Terminal(String),
}

impl Error {
    fn exit_code(&self) -> ExitCode {
        match self {
            Error::Config(_) => ExitCode::from(2),
            Error::Database(_) | Error::Auth(_) | Error::Terminal(_) => ExitCode::from(1),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Config(message)
            | Error::Database(message)
            | Error::Auth(message)
            | Error::Terminal(message) => f.write_str(message),
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    let result = match cli.command {
        // The UI needs a keyboard and a screen. Piped or in a script, print the help instead,
        // the way `ember` with no command always did.
        None if std::io::stdin().is_terminal() && std::io::stdout().is_terminal() => {
            tui::run().await
        }
        None => {
            eprint!("{}", Cli::command().render_help());
            return ExitCode::from(2);
        }
        Some(Command::Status) => status::run().await,
        Some(Command::Auth { command }) => match command {
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
