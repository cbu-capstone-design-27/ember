//! The terminal UI (EMBER-120): `ember` with no subcommand, in a terminal. A Status screen and a
//! Workspace screen. The sign-in is real; the rest is preview data until the API decision
//! (EMBER-115). `app.rs` holds the state and keys, `view.rs` and the screen files draw it.

mod app;
mod data;
mod status;
mod view;
mod workspace;

use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use chrono::Utc;
use ratatui::DefaultTerminal;
use ratatui::crossterm::event::{self, Event};

use crate::{Error, auth};
use app::App;
use data::{Account, Preview};

/// How long to wait for a key before drawing again, so the sign-in shows soon after it arrives.
const TICK: Duration = Duration::from_millis(100);

pub async fn run() -> Result<(), Error> {
    // Ask the web app who we are while the UI opens, so a slow or unreachable web app never
    // holds up the first screen. The answer arrives over a channel.
    let (send_account, account) = mpsc::channel();
    tokio::spawn(async move {
        let answer = match auth::signed_in().await {
            Ok(Some(me)) => Account::SignedIn(me),
            Ok(None) => Account::NotSignedIn,
            Err(error) => Account::Unavailable(error.to_string()),
        };
        // Err only means the UI already closed, so nobody needs the answer
        let _ = send_account.send(answer);
    });

    let app = App::new(Preview::stub(Utc::now()));
    // Waiting for keys blocks, so the UI gets a thread of its own. That leaves this one free to
    // finish the request above.
    tokio::task::spawn_blocking(move || show(app, account))
        .await
        .map_err(|error| Error::Terminal(format!("The terminal UI stopped: {error}")))?
}

/// Takes over the terminal until the user quits, then gives it back as it was.
fn show(app: App, account: Receiver<Account>) -> Result<(), Error> {
    // Raw mode and the alternate screen, plus a panic hook that undoes both before a panic prints
    let mut terminal = ratatui::try_init().map_err(terminal_error)?;
    let result = event_loop(&mut terminal, app, account);
    ratatui::restore();
    result.map_err(terminal_error)
}

fn event_loop(
    terminal: &mut DefaultTerminal,
    mut app: App,
    account: Receiver<Account>,
) -> std::io::Result<()> {
    while !app.quit {
        if let Ok(answer) = account.try_recv() {
            app.account = answer;
        }
        terminal.draw(|frame| view::draw(frame, &app))?;
        // A resize also ends the wait, and the next draw fits the new size
        if event::poll(TICK)?
            && let Event::Key(key) = event::read()?
        {
            app.on_key(key);
        }
    }
    Ok(())
}

fn terminal_error(error: std::io::Error) -> Error {
    Error::Terminal(format!("Cannot use the terminal: {error}"))
}
