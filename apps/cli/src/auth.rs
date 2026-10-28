//! `ember auth login`, `status` and `logout` (EMBER-87): signing in to the web app from the
//! terminal. The contract is `apps/web/README.md`, section "CLI sign-in": the OAuth device flow
//! (RFC 8628), then `whoami` and `sign-out` with the token as a bearer.

use std::fmt;
use std::path::PathBuf;
use std::time::Duration;

use reqwest::{Client, StatusCode, Url};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::time::{Instant, sleep};

use crate::Error;

const DEFAULT_URL: &str = "http://localhost:3000";
const CLIENT_ID: &str = "ember-cli";
const DEVICE_GRANT: &str = "urn:ietf:params:oauth:grant-type:device_code";
const CREDENTIALS_FILE: &str = "credentials.json";

/// How long to wait for the web app to answer one request, like `db.rs`'s connect timeout.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

/// What `slow_down` adds to the polling interval (RFC 8628, section 3.5).
const SLOW_DOWN_STEP: Duration = Duration::from_secs(5);

const LOGIN_AGAIN: &str = "Run `ember auth login`";

/// What `login` leaves on disk: where the token came from, the token, and whose it is.
#[derive(Serialize, Deserialize)]
struct Credentials {
    url: String,
    token: String,
    email: String,
}

/// Written by hand so the token can never end up in a log or a panic message.
impl fmt::Debug for Credentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Credentials")
            .field("url", &self.url)
            .field("token", &"<hidden>")
            .field("email", &self.email)
            .finish()
    }
}

#[derive(Deserialize)]
struct DeviceCode {
    device_code: String,
    user_code: String,
    verification_uri: String,
    verification_uri_complete: String,
    expires_in: u64,
    interval: u64,
}

/// The answer to a poll. Either field can be missing, so both are optional.
#[derive(Deserialize, Default)]
struct TokenAnswer {
    access_token: Option<String>,
    error: Option<String>,
}

#[derive(Deserialize)]
struct WhoAmI {
    email: String,
}

// ---- login ----

pub async fn login(url: Option<String>, no_browser: bool) -> Result<(), Error> {
    let url = web_app_url(url)?;
    let client = client()?;

    let code: DeviceCode = {
        let response = client
            .post(format!("{url}/api/auth/device/code"))
            .json(&json!({ "client_id": CLIENT_ID }))
            .send()
            .await
            .map_err(|error| unreachable(&url, &error))?;
        if !response.status().is_success() {
            return Err(unexpected("the sign-in request", response).await);
        }
        response.json().await.map_err(|_| {
            Error::Auth(format!(
                "{url} answered with something that isn't a sign-in code. Is it the Ember web app?"
            ))
        })?
    };

    println!("Your code is {}", format_user_code(&code.user_code));
    println!("Approve it at {}", code.verification_uri);
    if !no_browser {
        // The server picks this link, so only hand http(s) ones to the system
        let opened = is_http(&code.verification_uri_complete)
            && webbrowser::open(&code.verification_uri_complete).is_ok();
        if !opened {
            println!("Could not open a browser. Open the address above yourself.");
        }
    }
    println!("Waiting for you to approve it in the browser...");

    let token = wait_for_token(&client, &url, &code).await?;
    let email = who_am_i(&client, &url, &token).await?;
    let credentials = Credentials { url, token, email };
    save(&credentials)?;
    println!("Successfully authenticated as {}", credentials.email);
    Ok(())
}

/// Asks for the token every `interval` seconds until the user decides or the code runs out.
async fn wait_for_token(client: &Client, url: &str, code: &DeviceCode) -> Result<String, Error> {
    let expired = || Error::Auth(format!("The code expired. {LOGIN_AGAIN} again"));
    let deadline = Instant::now() + Duration::from_secs(code.expires_in);
    let mut interval = Duration::from_secs(code.interval);
    loop {
        sleep(interval).await;
        if Instant::now() >= deadline {
            return Err(expired());
        }
        let response = client
            .post(format!("{url}/api/auth/device/token"))
            .json(&json!({
                "grant_type": DEVICE_GRANT,
                "device_code": code.device_code,
                "client_id": CLIENT_ID,
            }))
            .send()
            .await
            .map_err(|error| unreachable(url, &error))?;
        let status = response.status();
        // A body that isn't JSON counts as "no error given"
        let answer: TokenAnswer = response.json().await.unwrap_or_default();
        if let (true, Some(token)) = (status.is_success(), answer.access_token) {
            return Ok(token);
        }
        match answer.error.as_deref() {
            Some("authorization_pending") => {}
            Some("slow_down") => interval += SLOW_DOWN_STEP,
            Some("access_denied") => return Err(Error::Auth("Sign-in denied".into())),
            Some("expired_token") => return Err(expired()),
            Some("invalid_grant") => {
                return Err(Error::Auth(format!(
                    "The code is unknown or was already used. {LOGIN_AGAIN} again"
                )));
            }
            other => {
                return Err(Error::Auth(format!(
                    "{url} answered the sign-in with HTTP {}{}",
                    status.as_u16(),
                    other
                        .map(|e| format!(" ({})", plain(e)))
                        .unwrap_or_default()
                )));
            }
        }
    }
}

// ---- status and logout ----

pub async fn status() -> Result<(), Error> {
    let Some(credentials) = load()? else {
        return Err(not_logged_in());
    };
    let email = who_am_i(&client()?, &credentials.url, &credentials.token).await?;
    println!("Logged in to {} as {email}", credentials.url);
    Ok(())
}

pub async fn logout() -> Result<(), Error> {
    let credentials = match load() {
        Ok(Some(credentials)) => credentials,
        Ok(None) => {
            println!("Not logged in");
            return Ok(());
        }
        Err(Error::Auth(problem)) => {
            // Unreadable, so there's no token to send; clearing it is the point of logging out
            remove_file()?;
            eprintln!("warning: {problem} Deleted it.");
            println!("Logged out");
            return Ok(());
        }
        Err(other) => return Err(other),
    };
    // Best effort: whatever the server says, the token should not stay on this machine
    let url = &credentials.url;
    let ended = client()?
        .post(format!("{url}/api/auth/sign-out"))
        .bearer_auth(&credentials.token)
        .json(&json!({}))
        .send()
        .await;
    match ended {
        Ok(response) if response.status().is_success() => {}
        Ok(response) if response.status() == StatusCode::UNAUTHORIZED => {
            eprintln!("warning: {url} no longer accepted the token, so it had already ended.");
        }
        Ok(response) => eprintln!(
            "warning: {url} answered HTTP {}, so the session may still be open there.",
            response.status().as_u16()
        ),
        Err(error) => eprintln!(
            "warning: {}, so the session may still be open there.",
            unreachable(url, &error)
        ),
    }
    remove_file()?;
    println!("Logged out of {url}");
    Ok(())
}

/// Asks the web app who owns `token`. A 401 means it expired or was revoked, so we are not logged in.
async fn who_am_i(client: &Client, url: &str, token: &str) -> Result<String, Error> {
    let response = client
        .get(format!("{url}/api/whoami"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|error| unreachable(url, &error))?;
    if response.status() == StatusCode::UNAUTHORIZED {
        return Err(not_logged_in());
    }
    if !response.status().is_success() {
        return Err(unexpected("the whoami request", response).await);
    }
    let who: WhoAmI = response.json().await.map_err(|_| {
        Error::Auth(format!(
            "{url} answered whoami with something unexpected. Is it the Ember web app?"
        ))
    })?;
    Ok(who.email)
}

// ---- the web app's address ----

/// `--url`, else EMBER_URL, else the local dev server. Returns it without a trailing slash.
fn web_app_url(flag: Option<String>) -> Result<String, Error> {
    let from_env = std::env::var("EMBER_URL").ok();
    let chosen = [flag, from_env]
        .into_iter()
        .flatten()
        .map(|value| value.trim().to_string())
        .find(|value| !value.is_empty());
    parse_url(chosen.as_deref().unwrap_or(DEFAULT_URL))
}

/// Checks that `url` is an http(s) address. Errors don't repeat it, in case it holds a password.
fn parse_url(url: &str) -> Result<String, Error> {
    let bad = |why: &str| {
        Error::Config(format!(
            "The web app address {why}. Use --url or EMBER_URL, e.g. {DEFAULT_URL}"
        ))
    };
    let parsed = Url::parse(url).map_err(|_| bad("is not a valid URL"))?;
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return Err(bad("must be an http:// or https:// URL"));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(bad("must not contain a user name or password"));
    }
    Ok(url.trim_end_matches('/').to_string())
}

fn is_http(url: &str) -> bool {
    Url::parse(url).is_ok_and(|url| matches!(url.scheme(), "http" | "https"))
}

// ---- the credentials file ----

/// `EMBER_CONFIG_DIR`, else `ember` inside the platform's config directory.
fn config_dir() -> Result<PathBuf, Error> {
    if let Some(dir) = std::env::var_os("EMBER_CONFIG_DIR").filter(|dir| !dir.is_empty()) {
        return Ok(PathBuf::from(dir));
    }
    dirs::config_dir()
        .map(|dir| dir.join("ember"))
        .ok_or_else(|| {
            Error::Config("Cannot find your config directory. Set EMBER_CONFIG_DIR.".into())
        })
}

fn credentials_path() -> Result<PathBuf, Error> {
    Ok(config_dir()?.join(CREDENTIALS_FILE))
}

/// The stored sign-in, or None if there isn't one.
fn load() -> Result<Option<Credentials>, Error> {
    let path = credentials_path()?;
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(Error::Config(format!(
                "Cannot read {}: {error}",
                path.display()
            )));
        }
    };
    serde_json::from_str(&text).map(Some).map_err(|_| {
        Error::Auth(format!(
            "{} is damaged. {LOGIN_AGAIN} to replace it.",
            path.display()
        ))
    })
}

/// Writes the credentials file so nobody else can ever read it: the temp file is created
/// with mode 0600 (not chmod-ed afterwards), then renamed over the old file in one step.
fn save(credentials: &Credentials) -> Result<(), Error> {
    use std::io::Write;

    let path = credentials_path()?;
    let dir = path
        .parent()
        .expect("the credentials file is inside a directory");
    let failed = |what: &str, error: std::io::Error| {
        Error::Config(format!("Cannot {what} {}: {error}", dir.display()))
    };

    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(dir).map_err(|e| failed("create", e))?;

    let temp = dir.join(format!("{CREDENTIALS_FILE}.tmp"));
    let _ = std::fs::remove_file(&temp);
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let json = serde_json::to_vec_pretty(credentials).expect("credentials are plain strings");
    let written = options
        .open(&temp)
        .and_then(|mut file| file.write_all(&json).and_then(|()| file.sync_all()))
        .and_then(|()| std::fs::rename(&temp, &path));
    if let Err(error) = written {
        let _ = std::fs::remove_file(&temp);
        return Err(failed("write the sign-in in", error));
    }
    Ok(())
}

fn remove_file() -> Result<(), Error> {
    let path = credentials_path()?;
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(Error::Config(format!(
            "Cannot delete {}: {error}",
            path.display()
        ))),
    }
}

// ---- small helpers ----

fn client() -> Result<Client, Error> {
    Client::builder()
        .timeout(REQUEST_TIMEOUT)
        .user_agent(concat!("ember-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|error| Error::Auth(format!("Cannot set up HTTP: {error}")))
}

fn not_logged_in() -> Error {
    Error::Auth(format!("Not logged in. {LOGIN_AGAIN}."))
}

/// "WDJBMJHT" as "WDJB-MJHT", the way the web app's page shows it.
fn format_user_code(code: &str) -> String {
    if code.len() == 8 && code.is_ascii() && !code.contains('-') {
        format!("{}-{}", &code[..4], &code[4..])
    } else {
        code.to_string()
    }
}

/// Keeps only letters, digits and underscores, so text the server sent can't mess with the terminal.
fn plain(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_')
        .take(40)
        .collect()
}

/// A request that got no answer: refused, timed out, DNS failure. Names the innermost cause.
fn unreachable(url: &str, error: &reqwest::Error) -> Error {
    let mut cause: &dyn std::error::Error = error;
    while let Some(next) = cause.source() {
        cause = next;
    }
    let why = if error.is_timeout() {
        format!("no answer after {}s", REQUEST_TIMEOUT.as_secs())
    } else {
        cause.to_string()
    };
    Error::Auth(format!("Cannot reach the web app at {url}: {why}"))
}

/// A status the contract doesn't list. Includes the `error` field if the body has one.
async fn unexpected(what: &str, response: reqwest::Response) -> Error {
    let status = response.status().as_u16();
    let answer: TokenAnswer = response.json().await.unwrap_or_default();
    let detail = answer
        .error
        .map(|error| format!(" ({})", plain(&error)))
        .unwrap_or_default();
    Error::Auth(format!(
        "The web app answered {what} with HTTP {status}{detail}"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config_error(url: &str) -> String {
        match parse_url(url) {
            Err(Error::Config(message)) => message,
            other => panic!("expected a config error for {url:?}, got {other:?}"),
        }
    }

    #[test]
    fn user_code_gets_a_dash() {
        assert_eq!(format_user_code("WDJBMJHT"), "WDJB-MJHT");
        assert_eq!(format_user_code("WDJB-MJHT"), "WDJB-MJHT");
        assert_eq!(format_user_code("ABC"), "ABC");
    }

    #[test]
    fn takes_http_and_https_and_drops_the_trailing_slash() {
        assert_eq!(
            parse_url("http://localhost:3000").unwrap(),
            "http://localhost:3000"
        );
        assert_eq!(
            parse_url("https://ember.example/").unwrap(),
            "https://ember.example"
        );
    }

    #[test]
    fn refuses_other_schemes_and_junk() {
        assert!(config_error("ftp://ember.example").contains("http:// or https://"));
        assert!(config_error("file:///etc/passwd").contains("http:// or https://"));
        assert!(config_error("localhost:3000").contains("http:// or https://"));
        assert!(config_error("not a url").contains("not a valid URL"));
    }

    #[test]
    fn url_with_a_password_is_refused_without_echoing_it() {
        let message = config_error("https://ada:hunter2@ember.example");
        assert!(!message.contains("hunter2"), "{message}");
    }

    #[test]
    fn debug_hides_the_token() {
        let credentials = Credentials {
            url: "http://localhost:3000".into(),
            token: "s3cret-token".into(),
            email: "ada@example.com".into(),
        };
        assert!(!format!("{credentials:?}").contains("s3cret-token"));
    }

    #[test]
    fn server_text_is_cleaned_before_printing() {
        assert_eq!(plain("bad\x1b[31m_thing"), "bad31m_thing");
    }
}
