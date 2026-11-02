//! The terminal UI's state and what each key does. No terminal in here, so it tests as plain
//! data: hand `on_key` a key, check the state.

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

use super::data::{Account, Preview};

/// The screens, in tab order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Screen {
    Status,
    Workspace,
}

impl Screen {
    pub const ALL: [Screen; 2] = [Screen::Status, Screen::Workspace];

    pub fn title(self) -> &'static str {
        match self {
            Screen::Status => "Status",
            Screen::Workspace => "Workspace",
        }
    }

    fn index(self) -> usize {
        Screen::ALL
            .iter()
            .position(|s| *s == self)
            .expect("every screen is in ALL")
    }

    /// The screen `step` tabs away, wrapping around at either end.
    fn moved(self, step: isize) -> Screen {
        let count = Screen::ALL.len() as isize;
        let index = (self.index() as isize + step).rem_euclid(count);
        Screen::ALL[index as usize]
    }
}

pub struct App {
    pub screen: Screen,
    pub account: Account,
    pub preview: Preview,
    pub quit: bool,
}

impl App {
    pub fn new(preview: Preview) -> App {
        App {
            screen: Screen::Status,
            account: Account::Checking,
            preview,
            quit: false,
        }
    }

    pub fn on_key(&mut self, key: KeyEvent) {
        // Windows also reports releases; act on presses only, so each key counts once
        if key.kind != KeyEventKind::Press {
            return;
        }
        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => self.quit = true,
            // Raw mode turns Ctrl+C into a key instead of a signal, so it has to quit here
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => self.quit = true,
            KeyCode::Tab | KeyCode::Right | KeyCode::Char('l') => {
                self.screen = self.screen.moved(1)
            }
            KeyCode::BackTab | KeyCode::Left | KeyCode::Char('h') => {
                self.screen = self.screen.moved(-1)
            }
            KeyCode::Char(digit @ '1'..='9') => {
                let index = digit as usize - '1' as usize;
                if let Some(screen) = Screen::ALL.get(index) {
                    self.screen = *screen;
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use chrono::Utc;
    use ratatui::crossterm::event::KeyEventState;

    use super::*;

    fn app() -> App {
        App::new(Preview::stub(Utc::now()))
    }

    fn press(app: &mut App, code: KeyCode) {
        app.on_key(KeyEvent::new(code, KeyModifiers::NONE));
    }

    #[test]
    fn opens_on_status_while_checking_the_sign_in() {
        let app = app();
        assert_eq!(app.screen, Screen::Status);
        assert_eq!(app.account, Account::Checking);
        assert!(!app.quit);
    }

    #[test]
    fn tab_and_arrows_move_between_screens_and_wrap() {
        let mut app = app();
        press(&mut app, KeyCode::Tab);
        assert_eq!(app.screen, Screen::Workspace);
        press(&mut app, KeyCode::Tab);
        assert_eq!(app.screen, Screen::Status, "Tab wraps past the last screen");
        press(&mut app, KeyCode::Left);
        assert_eq!(
            app.screen,
            Screen::Workspace,
            "Left wraps past the first screen"
        );
        press(&mut app, KeyCode::BackTab);
        assert_eq!(app.screen, Screen::Status);
        press(&mut app, KeyCode::Right);
        assert_eq!(app.screen, Screen::Workspace);
    }

    #[test]
    fn number_keys_jump_to_a_screen_and_ignore_ones_past_the_end() {
        let mut app = app();
        press(&mut app, KeyCode::Char('2'));
        assert_eq!(app.screen, Screen::Workspace);
        press(&mut app, KeyCode::Char('9'));
        assert_eq!(app.screen, Screen::Workspace);
        press(&mut app, KeyCode::Char('1'));
        assert_eq!(app.screen, Screen::Status);
    }

    #[test]
    fn q_esc_and_ctrl_c_quit() {
        for key in [
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE),
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
            KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
        ] {
            let mut app = app();
            app.on_key(key);
            assert!(app.quit, "{key:?} should quit");
        }
    }

    #[test]
    fn plain_c_does_not_quit() {
        let mut app = app();
        press(&mut app, KeyCode::Char('c'));
        assert!(!app.quit);
    }

    #[test]
    fn key_releases_are_ignored() {
        let mut app = app();
        app.on_key(KeyEvent {
            code: KeyCode::Char('q'),
            modifiers: KeyModifiers::NONE,
            kind: KeyEventKind::Release,
            state: KeyEventState::NONE,
        });
        assert!(!app.quit);
    }
}
