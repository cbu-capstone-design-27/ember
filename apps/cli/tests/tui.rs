//! `ember` with no command (EMBER-120). In a terminal it opens the terminal UI; anywhere else
//! (a pipe, a script, CI) it must print the help instead of taking over a screen that isn't
//! there. The UI itself is tested in `src/tui/`, drawn on ratatui's test backend.

use std::process::{Command, Stdio};

#[test]
fn without_a_terminal_plain_ember_prints_the_help_and_exits_2() {
    // Captured stdout and a closed stdin: neither is a terminal
    let output = Command::new(env!("CARGO_BIN_EXE_ember"))
        .stdin(Stdio::null())
        .output()
        .expect("run ember");
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty(), "the UI must not draw into a pipe");
    let help = String::from_utf8_lossy(&output.stderr);
    assert!(help.contains("Usage: ember"), "{help}");
    assert!(help.contains("terminal UI"), "{help}");
    for command in ["status", "auth"] {
        assert!(
            help.contains(command),
            "help should list `{command}`: {help}"
        );
    }
    // Raw mode and the alternate screen are escape codes; none should have been written
    assert!(!help.contains('\x1b'), "{help:?}");
}
