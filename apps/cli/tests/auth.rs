//! `ember auth` end to end: the built binary against a fake web app on 127.0.0.1.
//!
//! No database is needed. Every run gets its own EMBER_CONFIG_DIR and `--no-browser`.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

const TOKEN: &str = "tok_9f8e7d6c5b4a";
const EMAIL: &str = "ada@example.com";

/// One request the fake received.
#[derive(Clone, Debug)]
struct Request {
    method: String,
    path: String,
    /// Header names lowercased.
    headers: Vec<(String, String)>,
    body: String,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }
}

/// A web app that answers each request with `handler(request, how many came before it)`.
struct FakeWebApp {
    url: String,
    requests: Arc<Mutex<Vec<Request>>>,
}

impl FakeWebApp {
    fn start(handler: impl Fn(&Request, usize) -> (u16, String) + Send + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let seen = Arc::clone(&requests);
        // Runs until the test process exits
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let request = read_request(&mut stream);
                let before = {
                    let mut seen = seen.lock().unwrap();
                    seen.push(request.clone());
                    seen.len() - 1
                };
                let (status, body) = handler(&request, before);
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\n\
                     content-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        FakeWebApp { url, requests }
    }

    fn requests(&self) -> Vec<Request> {
        self.requests.lock().unwrap().clone()
    }

    fn requests_to(&self, path: &str) -> Vec<Request> {
        self.requests()
            .into_iter()
            .filter(|r| r.path == path)
            .collect()
    }
}

fn read_request(stream: &mut std::net::TcpStream) -> Request {
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();
    let mut headers = Vec::new();
    loop {
        line.clear();
        reader.read_line(&mut line).unwrap();
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.to_ascii_lowercase(), value.trim().to_string()));
        }
    }
    let length = headers
        .iter()
        .find(|(name, _)| name == "content-length")
        .and_then(|(_, value)| value.parse().ok())
        .unwrap_or(0);
    let mut body = vec![0; length];
    reader.read_exact(&mut body).unwrap();
    Request {
        method,
        path,
        headers,
        body: String::from_utf8_lossy(&body).into_owned(),
    }
}

/// A device code with a polling interval of 0, so the tests don't wait.
fn device_code_body() -> String {
    r#"{"device_code":"dev-code-123","user_code":"WDJBMJHT",
        "verification_uri":"http://web.example/device",
        "verification_uri_complete":"http://web.example/device?user_code=WDJBMJHT",
        "expires_in":1800,"interval":0}"#
        .to_string()
}

fn token_error(error: &str) -> (u16, String) {
    (400, format!(r#"{{"error":"{error}"}}"#))
}

fn token_ok() -> (u16, String) {
    (
        200,
        format!(
            r#"{{"access_token":"{TOKEN}","token_type":"Bearer","expires_in":604800,"scope":""}}"#
        ),
    )
}

/// A web app where the device-code step works and the token endpoint answers `poll(n)` for the
/// n-th poll. `whoami` and `sign-out` accept only TOKEN.
fn web_app(poll: impl Fn(usize) -> (u16, String) + Send + 'static) -> FakeWebApp {
    let polls = AtomicUsize::new(0);
    FakeWebApp::start(move |request, _| match request.path.as_str() {
        "/api/auth/device/code" => (200, device_code_body()),
        "/api/auth/device/token" => poll(polls.fetch_add(1, Ordering::SeqCst)),
        "/api/whoami" => whoami(request),
        "/api/auth/sign-out" => sign_out(request),
        _ => (404, "{}".into()),
    })
}

fn authorized(request: &Request) -> bool {
    request.header("authorization") == Some(&format!("Bearer {TOKEN}"))
}

fn whoami(request: &Request) -> (u16, String) {
    if authorized(request) {
        (
            200,
            format!(r#"{{"name":"Ada Lovelace","email":"{EMAIL}"}}"#),
        )
    } else {
        (401, r#"{"error":"unauthorized"}"#.into())
    }
}

fn sign_out(request: &Request) -> (u16, String) {
    if authorized(request) {
        (200, r#"{"success":true}"#.into())
    } else {
        (401, r#"{"error":"unauthorized"}"#.into())
    }
}

fn ember(config_dir: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_ember"))
        .args(args)
        .env("EMBER_CONFIG_DIR", config_dir)
        .env_remove("EMBER_URL")
        .output()
        .expect("run ember")
}

fn login(config_dir: &Path, url: &str) -> Output {
    ember(config_dir, &["auth", "login", "--no-browser", "--url", url])
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn credentials_path(config_dir: &Path) -> PathBuf {
    config_dir.join("credentials.json")
}

/// Puts a sign-in for `url` in the config directory, as `login` would.
fn sign_in_with(config_dir: &Path, url: &str, token: &str) {
    std::fs::write(
        credentials_path(config_dir),
        format!(r#"{{"url":"{url}","token":"{token}","email":"{EMAIL}"}}"#),
    )
    .unwrap();
}

fn assert_no_token(output: &Output) {
    assert!(!stdout(output).contains(TOKEN), "token in stdout");
    assert!(!stderr(output).contains(TOKEN), "token in stderr");
}

fn bearer() -> String {
    format!("Bearer {TOKEN}")
}

/// A localhost port nothing listens on.
fn dead_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    format!("http://{}", listener.local_addr().unwrap())
}

#[cfg(unix)]
fn mode(path: &Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path).unwrap().permissions().mode() & 0o777
}

// ---- login ----

#[test]
fn login_waits_for_approval_then_stores_the_token() {
    let app = web_app(|n| {
        if n < 2 {
            token_error("authorization_pending")
        } else {
            token_ok()
        }
    });
    let dir = tempfile::tempdir().unwrap();
    let output = login(dir.path(), &app.url);

    assert!(output.status.success(), "{}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("WDJB-MJHT"), "{out}");
    assert!(out.contains("http://web.example/device"), "{out}");
    assert!(
        out.contains(&format!("Successfully authenticated as {EMAIL}")),
        "{out}"
    );
    assert_no_token(&output);

    let saved: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(credentials_path(dir.path())).unwrap())
            .unwrap();
    assert_eq!(saved["token"], TOKEN);
    assert_eq!(saved["email"], EMAIL);
    assert_eq!(saved["url"], app.url.as_str());
    #[cfg(unix)]
    assert_eq!(mode(&credentials_path(dir.path())), 0o600);

    // The requests follow the web app's contract, and carry no cookie or Origin
    let code = app.requests_to("/api/auth/device/code");
    assert_eq!(code[0].method, "POST");
    assert!(
        code[0].body.contains(r#""client_id":"ember-cli""#),
        "{}",
        code[0].body
    );
    let polls = app.requests_to("/api/auth/device/token");
    assert_eq!(polls.len(), 3);
    assert!(
        polls[0]
            .body
            .contains("urn:ietf:params:oauth:grant-type:device_code")
    );
    assert!(polls[0].body.contains("dev-code-123"));
    for request in app.requests() {
        assert!(request.header("cookie").is_none());
        assert!(request.header("origin").is_none());
    }
    let who = app.requests_to("/api/whoami");
    assert_eq!(who[0].header("authorization"), Some(bearer().as_str()));
}

#[test]
fn login_creates_a_missing_config_directory() {
    let app = web_app(|_| token_ok());
    let dir = tempfile::tempdir().unwrap();
    let nested = dir.path().join("a").join("ember");
    let output = login(&nested, &app.url);
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(credentials_path(&nested).exists());
    #[cfg(unix)]
    assert_eq!(mode(&nested), 0o700);
}

#[test]
fn login_replaces_an_earlier_sign_in() {
    let app = web_app(|_| token_ok());
    let dir = tempfile::tempdir().unwrap();
    sign_in_with(dir.path(), "http://old.example", "old-token");
    let output = login(dir.path(), &app.url);
    assert!(output.status.success(), "{}", stderr(&output));
    let saved = std::fs::read_to_string(credentials_path(dir.path())).unwrap();
    assert!(
        saved.contains(TOKEN) && !saved.contains("old-token"),
        "{saved}"
    );
}

#[test]
fn slow_down_makes_it_wait_longer_and_still_succeeds() {
    let app = web_app(|n| {
        if n == 0 {
            token_error("slow_down")
        } else {
            token_ok()
        }
    });
    let dir = tempfile::tempdir().unwrap();
    let started = std::time::Instant::now();
    let output = login(dir.path(), &app.url);
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(stdout(&output).contains("Successfully authenticated"));
    // The fake asks for no wait at all; slow_down adds 5 s before every poll after it
    assert!(started.elapsed().as_secs() >= 5, "{:?}", started.elapsed());
}

#[test]
fn denied_sign_in_stops() {
    let app = web_app(|_| token_error("access_denied"));
    let dir = tempfile::tempdir().unwrap();
    let output = login(dir.path(), &app.url);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(stderr(&output), "error: Sign-in denied\n");
    assert!(!credentials_path(dir.path()).exists());
}

#[test]
fn expired_code_says_to_log_in_again() {
    let app = web_app(|_| token_error("expired_token"));
    let dir = tempfile::tempdir().unwrap();
    let output = login(dir.path(), &app.url);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        stderr(&output),
        "error: The code expired. Run `ember auth login` again\n"
    );
    assert!(!credentials_path(dir.path()).exists());
}

#[test]
fn used_code_stops() {
    let app = web_app(|_| token_error("invalid_grant"));
    let dir = tempfile::tempdir().unwrap();
    let output = login(dir.path(), &app.url);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        stderr(&output).contains("unknown or was already used"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn unknown_client_is_reported_with_its_status() {
    let app = FakeWebApp::start(|_, _| (400, r#"{"error":"invalid_client"}"#.into()));
    let dir = tempfile::tempdir().unwrap();
    let output = login(dir.path(), &app.url);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        stderr(&output),
        "error: The web app answered the sign-in request with HTTP 400 (invalid_client)\n"
    );
}

#[test]
fn unexpected_poll_status_stops() {
    let app = web_app(|_| (500, "boom".into()));
    let dir = tempfile::tempdir().unwrap();
    let output = login(dir.path(), &app.url);
    assert_eq!(output.status.code(), Some(1));
    assert!(stderr(&output).contains("HTTP 500"), "{}", stderr(&output));
}

#[test]
fn unreachable_server_exits_1() {
    let dir = tempfile::tempdir().unwrap();
    let url = dead_url();
    let output = login(dir.path(), &url);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        stderr(&output).starts_with(&format!("error: Cannot reach the web app at {url}")),
        "{}",
        stderr(&output)
    );
}

#[test]
fn bad_url_is_a_config_error() {
    let dir = tempfile::tempdir().unwrap();
    for url in ["ftp://ember.example", "localhost:3000", "nonsense"] {
        let output = login(dir.path(), url);
        assert_eq!(output.status.code(), Some(2), "{url}");
        assert!(
            stderr(&output).contains("web app address"),
            "{}",
            stderr(&output)
        );
    }
}

#[test]
fn bad_ember_url_is_a_config_error_and_the_flag_wins_over_it() {
    let dir = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_ember"))
        .args(["auth", "login", "--no-browser"])
        .env("EMBER_CONFIG_DIR", dir.path())
        .env("EMBER_URL", "gopher://x")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));

    // With --url given, EMBER_URL isn't even looked at
    let app = web_app(|_| token_ok());
    let output = Command::new(env!("CARGO_BIN_EXE_ember"))
        .args(["auth", "login", "--no-browser", "--url", &app.url])
        .env("EMBER_CONFIG_DIR", dir.path())
        .env("EMBER_URL", "gopher://x")
        .output()
        .unwrap();
    assert!(output.status.success(), "{}", stderr(&output));
}

// ---- status ----

#[test]
fn status_names_the_account_and_server() {
    let app = web_app(|_| token_ok());
    let dir = tempfile::tempdir().unwrap();
    sign_in_with(dir.path(), &app.url, TOKEN);
    let output = ember(dir.path(), &["auth", "status"]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(
        stdout(&output),
        format!("Logged in to {} as {EMAIL}\n", app.url)
    );
    assert_no_token(&output);
}

#[test]
fn status_with_a_rejected_token_keeps_the_file() {
    let app = web_app(|_| token_ok());
    let dir = tempfile::tempdir().unwrap();
    sign_in_with(dir.path(), &app.url, "revoked-token");
    let output = ember(dir.path(), &["auth", "status"]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        stderr(&output),
        "error: Not logged in. Run `ember auth login`.\n"
    );
    assert!(!stderr(&output).contains("revoked-token"));
    assert!(credentials_path(dir.path()).exists());
}

#[test]
fn status_when_not_signed_in() {
    let dir = tempfile::tempdir().unwrap();
    let output = ember(dir.path(), &["auth", "status"]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        stderr(&output),
        "error: Not logged in. Run `ember auth login`.\n"
    );
}

#[test]
fn status_with_a_damaged_file_says_so() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(credentials_path(dir.path()), "{ not json").unwrap();
    let output = ember(dir.path(), &["auth", "status"]);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        stderr(&output).contains("is damaged"),
        "{}",
        stderr(&output)
    );
}

// ---- logout ----

#[test]
fn logout_ends_the_session_and_deletes_the_file() {
    let app = web_app(|_| token_ok());
    let dir = tempfile::tempdir().unwrap();
    sign_in_with(dir.path(), &app.url, TOKEN);
    let output = ember(dir.path(), &["auth", "logout"]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(stdout(&output), format!("Logged out of {}\n", app.url));
    assert_eq!(stderr(&output), "");
    assert!(!credentials_path(dir.path()).exists());

    let calls = app.requests_to("/api/auth/sign-out");
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].method, "POST");
    assert_eq!(calls[0].header("authorization"), Some(bearer().as_str()));
    assert_eq!(calls[0].body, "{}");
}

#[test]
fn logout_deletes_the_file_even_when_the_server_is_down() {
    let dir = tempfile::tempdir().unwrap();
    let url = dead_url();
    sign_in_with(dir.path(), &url, TOKEN);
    let output = ember(dir.path(), &["auth", "logout"]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(stdout(&output), format!("Logged out of {url}\n"));
    assert!(
        stderr(&output).contains("Cannot reach the web app"),
        "{}",
        stderr(&output)
    );
    assert!(!credentials_path(dir.path()).exists());
}

#[test]
fn logout_deletes_the_file_when_the_token_is_already_dead() {
    let app = web_app(|_| token_ok());
    let dir = tempfile::tempdir().unwrap();
    sign_in_with(dir.path(), &app.url, "revoked-token");
    let output = ember(dir.path(), &["auth", "logout"]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("no longer accepted"),
        "{}",
        stderr(&output)
    );
    assert!(!credentials_path(dir.path()).exists());
}

#[test]
fn logout_when_not_signed_in_succeeds() {
    let dir = tempfile::tempdir().unwrap();
    let output = ember(dir.path(), &["auth", "logout"]);
    assert!(output.status.success());
    assert_eq!(stdout(&output), "Not logged in\n");
}
