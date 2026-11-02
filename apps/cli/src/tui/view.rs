//! Drawing the terminal UI: the tab bar and key hints around whichever screen is open
//! (`status.rs`, `workspace.rs`), plus the formatting they share.

use chrono::{DateTime, Utc};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout};
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::Tabs;

use super::app::{App, Screen};
use super::data::Account;
use super::{status, workspace};

/// Shown under every block of made-up data, so nobody mistakes it for theirs.
pub const PREVIEW_NOTE: &str = " preview data until the Ember API lands (EMBER-115) ";

pub fn draw(frame: &mut Frame, app: &App) {
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(1),
    ])
    .areas(frame.area());

    let [name_area, tabs_area, account_area] = Layout::horizontal([
        Constraint::Length(7),
        Constraint::Min(0),
        Constraint::Length(40),
    ])
    .areas(header);
    frame.render_widget(Span::from(" ember ").bold().fg(Color::LightRed), name_area);
    let tabs = Tabs::new(
        Screen::ALL
            .iter()
            .enumerate()
            .map(|(i, screen)| format!("{} {}", i + 1, screen.title())),
    )
    .select(Screen::ALL.iter().position(|s| *s == app.screen))
    .highlight_style(Style::new().bold().reversed())
    .divider(" ");
    frame.render_widget(tabs, tabs_area);
    frame.render_widget(account_summary(&app.account).right_aligned(), account_area);

    match app.screen {
        Screen::Status => status::draw(frame, body, app),
        Screen::Workspace => workspace::draw(frame, body, app),
    }

    frame.render_widget(
        Line::from(" Tab/←→ switch screens   1-2 jump   q quit ").dim(),
        footer,
    );
}

/// The top-right corner: who you are, in a word or two.
fn account_summary(account: &Account) -> Line<'static> {
    match account {
        Account::Checking => Line::from("checking sign-in… ").dim(),
        Account::SignedIn(me) => Line::from(format!("{} ", clean(&me.email))),
        Account::NotSignedIn => Line::from("not signed in ").yellow(),
        Account::Unavailable(_) => Line::from("sign-in unavailable ").yellow(),
    }
}

/// Text that came from a server, with control characters dropped so it can't move the cursor
/// or recolor the terminal.
pub fn clean(text: &str) -> String {
    text.chars().filter(|c| !c.is_control()).collect()
}

/// "1,284".
pub fn thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, digit) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

/// How long before `now` something happened: "just now", "4 min ago", "3 h ago", "2 d ago".
pub fn ago(then: DateTime<Utc>, now: DateTime<Utc>) -> String {
    let minutes = (now - then).num_minutes();
    match minutes {
        i64::MIN..1 => "just now".to_string(),
        1..60 => format!("{minutes} min ago"),
        60..1440 => format!("{} h ago", minutes / 60),
        _ => format!("{} d ago", minutes / 1440),
    }
}

#[cfg(test)]
pub mod tests {
    use chrono::{Duration, TimeZone};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    use super::*;
    use crate::auth::{Organization, SignedIn};
    use crate::tui::data::Preview;

    /// A fixed clock, so "4 min ago" is the same on every run.
    pub fn now() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 2, 16, 0, 0).unwrap()
    }

    pub fn app_on(screen: Screen, account: Account) -> App {
        let mut app = App::new(Preview::stub(now()));
        app.screen = screen;
        app.account = account;
        app
    }

    pub fn ada() -> Account {
        Account::SignedIn(SignedIn {
            url: "https://ember.example".into(),
            email: "ada@example.com".into(),
            organization: Some(Organization {
                name: "Kestrel Labs".into(),
                slug: "kestrel-labs".into(),
                tenant: Some("ember-development".into()),
                role: Some("owner".into()),
            }),
            organizations: Some(2),
        })
    }

    /// Draws `app` on a `width`×`height` screen and returns what's on it, one line per row.
    pub fn screen(app: &App, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| draw(frame, app)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| {
                let row: String = (0..width).map(|x| buffer[(x, y)].symbol()).collect();
                row.trim_end().to_string()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn tab_bar_names_every_screen_and_the_key_hints_show() {
        let text = screen(&app_on(Screen::Status, Account::Checking), 100, 24);
        let top = text.lines().next().unwrap();
        assert!(top.contains("ember"), "{top}");
        assert!(
            top.contains("1 Status") && top.contains("2 Workspace"),
            "{top}"
        );
        assert!(top.contains("checking sign-in"), "{top}");
        assert!(text.lines().last().unwrap().contains("q quit"), "{text}");
    }

    #[test]
    fn top_right_shows_who_is_signed_in() {
        let text = screen(&app_on(Screen::Status, ada()), 100, 24);
        assert!(
            text.lines().next().unwrap().contains("ada@example.com"),
            "{text}"
        );
        let text = screen(&app_on(Screen::Status, Account::NotSignedIn), 100, 24);
        assert!(
            text.lines().next().unwrap().contains("not signed in"),
            "{text}"
        );
    }

    #[test]
    fn a_tiny_terminal_draws_without_panicking() {
        for screen_kind in Screen::ALL {
            for (width, height) in [(1, 1), (20, 4), (40, 8)] {
                screen(&app_on(screen_kind, ada()), width, height);
            }
        }
    }

    #[test]
    fn server_text_loses_control_characters() {
        assert_eq!(clean("ada\x1b[31m@example.com\n"), "ada[31m@example.com");
    }

    #[test]
    fn thousands_get_commas() {
        assert_eq!(thousands(0), "0");
        assert_eq!(thousands(999), "999");
        assert_eq!(thousands(1_284), "1,284");
        assert_eq!(thousands(12_345_678), "12,345,678");
    }

    #[test]
    fn ages_read_naturally() {
        let now = now();
        assert_eq!(ago(now, now), "just now");
        assert_eq!(
            ago(now + Duration::minutes(5), now),
            "just now",
            "a clock ahead of ours"
        );
        assert_eq!(ago(now - Duration::minutes(4), now), "4 min ago");
        assert_eq!(ago(now - Duration::minutes(185), now), "3 h ago");
        assert_eq!(ago(now - Duration::days(2), now), "2 d ago");
    }
}
