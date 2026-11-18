//! `ember init` end to end (EMBER-123): the built binary in a temporary project directory.
//!
//! No database is needed. Every run gets its own empty EMBER_CONFIG_DIR, so it's not logged in,
//! except where a test signs in against a fake web app on 127.0.0.1.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Output};

const START: &str = "<!-- ember:start -->";
const END: &str = "<!-- ember:end -->";

struct Project {
    /// Holds the project directory and, beside it, the config directory.
    temp: tempfile::TempDir,
}

impl Project {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir(temp.path().join("project")).unwrap();
        std::fs::create_dir(temp.path().join("config")).unwrap();
        Project { temp }
    }

    fn dir(&self) -> std::path::PathBuf {
        self.temp.path().join("project")
    }

    fn config(&self) -> std::path::PathBuf {
        self.temp.path().join("config")
    }

    fn write(&self, name: &str, text: &str) {
        let path = self.dir().join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    fn read(&self, name: &str) -> String {
        std::fs::read_to_string(self.dir().join(name)).unwrap()
    }

    /// The project's files and their contents, to compare before and after a run.
    fn snapshot(&self) -> Vec<(String, Vec<u8>)> {
        let mut files: Vec<_> = std::fs::read_dir(self.dir())
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.is_file())
            .map(|path| {
                let name = path.file_name().unwrap().to_string_lossy().into_owned();
                (name, std::fs::read(&path).unwrap())
            })
            .collect();
        files.sort();
        files
    }

    fn init(&self, args: &[&str]) -> Output {
        self.init_in(&self.dir(), args)
    }

    fn init_in(&self, dir: &Path, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_ember"))
            .arg("init")
            .args(args)
            .current_dir(dir)
            .env("EMBER_CONFIG_DIR", self.config())
            .env_remove("EMBER_URL")
            .output()
            .expect("run ember")
    }
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn assert_ok(output: &Output) {
    assert!(
        output.status.success(),
        "exit {:?}\nstdout: {}\nstderr: {}",
        output.status.code(),
        stdout(output),
        stderr(output)
    );
}

/// The Ember section in `text`, markers included.
fn section(text: &str) -> &str {
    let start = text.find(START).expect("a start marker");
    let end = text.find(END).expect("an end marker") + END.len();
    &text[start..end]
}

#[test]
fn in_an_empty_directory_it_creates_both_files() {
    let project = Project::new();
    let output = project.init(&[]);
    assert_ok(&output);

    let agents = project.read("AGENTS.md");
    assert!(agents.starts_with(START), "{agents}");
    assert!(agents.ends_with(&format!("{END}\n")), "{agents}");
    assert!(agents.contains("`search`") && agents.contains("`get_record`"));
    assert_eq!(project.read("CLAUDE.md"), "@AGENTS.md\n");

    let out = stdout(&output);
    assert!(out.contains("Created AGENTS.md"), "{out}");
    assert!(out.contains("Created CLAUDE.md"), "{out}");
    assert!(!out.contains("git repository"), "not in a repo: {out}");
}

#[test]
fn in_a_project_with_both_files_it_only_adds_the_section_and_the_import() {
    let project = Project::new();
    let agents_before = "# Agent instructions\n\nRun `make test` before pushing.\n";
    let claude_before = "Prefer small commits.";
    project.write("AGENTS.md", agents_before);
    project.write("CLAUDE.md", claude_before);

    let output = project.init(&[]);
    assert_ok(&output);

    let agents = project.read("AGENTS.md");
    let added = agents
        .strip_prefix(agents_before)
        .expect("the user's text is untouched");
    assert_eq!(added, format!("\n{}\n", section(&agents)));
    assert_eq!(
        project.read("CLAUDE.md"),
        format!("{claude_before}\n\n@AGENTS.md\n")
    );

    let out = stdout(&output);
    assert!(
        out.contains("Added the Ember section to AGENTS.md"),
        "{out}"
    );
    assert!(out.contains("Added @AGENTS.md to CLAUDE.md"), "{out}");
}

#[test]
fn running_it_again_changes_nothing() {
    for existing in [false, true] {
        let project = Project::new();
        if existing {
            project.write("AGENTS.md", "# Mine\n");
            project.write("CLAUDE.md", "Mine too\n");
        }
        assert_ok(&project.init(&[]));
        let first = project.snapshot();

        let output = project.init(&[]);
        assert_ok(&output);
        assert_eq!(
            project.snapshot(),
            first,
            "byte-identical after the second run"
        );

        let out = stdout(&output);
        assert!(
            out.contains("AGENTS.md already has the current Ember section"),
            "{out}"
        );
        assert!(out.contains("CLAUDE.md already imports AGENTS.md"), "{out}");
    }
}

#[test]
fn an_older_section_is_replaced_in_place() {
    let project = Project::new();
    project.write(
        "AGENTS.md",
        &format!("# Mine\n\n{START}\nAn old Ember note.\n{END}\n\n## Also mine\n"),
    );
    project.write("CLAUDE.md", "@AGENTS.md\n");

    let output = project.init(&[]);
    assert_ok(&output);

    let agents = project.read("AGENTS.md");
    assert!(
        agents.starts_with(&format!("# Mine\n\n{START}\n")),
        "{agents}"
    );
    assert!(
        agents.ends_with(&format!("{END}\n\n## Also mine\n")),
        "{agents}"
    );
    assert!(!agents.contains("An old Ember note."), "{agents}");
    assert_eq!(agents.matches(START).count(), 1);
    assert_eq!(project.read("CLAUDE.md"), "@AGENTS.md\n");
    assert!(stdout(&output).contains("Updated the Ember section in AGENTS.md"));
}

#[test]
fn dry_run_writes_nothing_and_prints_the_plan() {
    // An empty directory, and one with files of its own
    let empty = Project::new();
    let output = empty.init(&["--dry-run"]);
    assert_ok(&output);
    assert!(empty.snapshot().is_empty(), "dry run created files");
    let out = stdout(&output);
    assert!(out.contains("Dry run: nothing is written."), "{out}");
    assert!(out.contains("Would create AGENTS.md:"), "{out}");
    assert!(out.contains(&format!("  + {START}")), "{out}");
    assert!(
        out.contains("Would create CLAUDE.md:\n  + @AGENTS.md"),
        "{out}"
    );

    let full = Project::new();
    full.write("AGENTS.md", &format!("# Mine\n\n{START}\nOld.\n{END}\n"));
    full.write("CLAUDE.md", "Mine\n");
    let before = full.snapshot();
    let output = full.init(&["--dry-run"]);
    assert_ok(&output);
    assert_eq!(full.snapshot(), before, "dry run changed files");
    let out = stdout(&output);
    assert!(
        out.contains("Would update the Ember section in AGENTS.md:"),
        "{out}"
    );
    assert!(out.contains("  - Old."), "{out}");
    assert!(
        out.contains("Would add to CLAUDE.md:\n  + @AGENTS.md"),
        "{out}"
    );
}

#[test]
fn inside_a_git_repo_it_writes_to_the_root_and_says_so() {
    let project = Project::new();
    std::fs::create_dir(project.dir().join(".git")).unwrap();
    let deep = project.dir().join("apps/web/src");
    std::fs::create_dir_all(&deep).unwrap();

    let output = project.init_in(&deep, &[]);
    assert_ok(&output);

    assert!(project.dir().join("AGENTS.md").is_file());
    assert!(project.dir().join("CLAUDE.md").is_file());
    assert!(!deep.join("AGENTS.md").exists());
    let root = std::fs::canonicalize(project.dir()).unwrap();
    let out = stdout(&output);
    assert!(
        out.contains(&format!(
            "Writing to the git repository's root, {}",
            root.display()
        )),
        "{out}"
    );
}

#[test]
fn not_logged_in_it_still_works_and_suggests_logging_in() {
    let project = Project::new();
    let output = project.init(&[]);
    assert_ok(&output);
    assert!(!project.read("AGENTS.md").contains("organization"));
    assert!(stdout(&output).contains(
        "Not logged in, so AGENTS.md doesn't name your organization. Run `ember auth login`"
    ));
}

#[test]
fn logged_in_it_names_the_organization() {
    let url = fake_whoami(
        r#"{"name":"Ada","email":"ada@example.com","organizations":[
            {"name":"Kestrel Labs","slug":"kestrel-labs","tenant":"ember-development","role":"owner"}
        ],"active":"kestrel-labs"}"#,
    );
    let project = Project::new();
    std::fs::write(
        project.config().join("credentials.json"),
        format!(r#"{{"url":"{url}","token":"tok_123","email":"ada@example.com"}}"#),
    )
    .unwrap();

    let output = project.init(&[]);
    assert_ok(&output);
    let agents = project.read("AGENTS.md");
    assert!(
        agents.contains("in the Kestrel Labs organization (`kestrel-labs`)"),
        "{agents}"
    );
    assert!(
        !stdout(&output).contains("ember auth login"),
        "{}",
        stdout(&output)
    );
}

#[test]
fn broken_markers_are_refused_and_nothing_is_written() {
    let project = Project::new();
    project.write("AGENTS.md", &format!("# Mine\n{START}\nno end marker\n"));
    let before = project.snapshot();

    let output = project.init(&[]);
    assert_eq!(output.status.code(), Some(2), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("one of each"),
        "{}",
        stderr(&output)
    );
    assert_eq!(
        project.snapshot(),
        before,
        "CLAUDE.md must not be created either"
    );
}

/// A web app whose `whoami` always answers `body`. Returns its address.
fn fake_whoami(body: &'static str) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    // Runs until the test process exits
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(&mut stream);
            let mut line = String::new();
            while reader.read_line(&mut line).is_ok_and(|n| n > 0) && line != "\r\n" {
                line.clear();
            }
            let _ = write!(
                stream,
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
                 content-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
        }
    });
    url
}
