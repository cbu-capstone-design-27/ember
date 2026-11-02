//! What the terminal UI shows. The sign-in is real (the web app's `/api/whoami`, as `ember auth
//! status` reads it). Everything else is preview data until the API decision (EMBER-115) says
//! where the CLI reads it from. Then `Preview::stub` gives way to a call to that API, and the
//! screens don't change.

use chrono::{DateTime, Duration, Utc};

use crate::auth::SignedIn;

/// The sign-in, which arrives a moment after the UI opens.
#[derive(Clone, Debug, PartialEq)]
pub enum Account {
    /// Still asking the web app.
    Checking,
    SignedIn(SignedIn),
    /// No token stored.
    NotSignedIn,
    /// The token was rejected, or the web app couldn't be reached. Holds the message to show.
    Unavailable(String),
}

/// One source's ingestion, as the Status screen lists it.
#[derive(Clone, Debug, PartialEq)]
pub struct SourceRow {
    pub source: String,
    pub records: u64,
    /// The newest record's time, in the source's own clock (`occurred_at`).
    pub newest: Option<DateTime<Utc>>,
    pub state: SourceState,
}

#[derive(Clone, Debug, PartialEq)]
pub enum SourceState {
    /// Webhooks arriving and enriched as they come.
    Live,
    /// Catching up on history.
    Backfilling {
        percent: u8,
    },
    NotConnected,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Member {
    pub name: String,
    pub email: String,
    pub role: String,
}

/// The data that isn't real yet.
#[derive(Clone, Debug, PartialEq)]
pub struct Preview {
    /// When it was read, so "4 min ago" stays the same however long the screen is open.
    pub as_of: DateTime<Utc>,
    pub sources: Vec<SourceRow>,
    pub members: Vec<Member>,
    /// Repositories, Jira projects and channels the organization ingests.
    pub projects: Vec<String>,
}

impl Preview {
    /// A made-up team: the Kestrel Labs of the web app's preview data
    /// (`apps/web/src/data/preview-workspace.json`), with its people, repositories and counts.
    pub fn stub(now: DateTime<Utc>) -> Preview {
        let source = |source: &str, records, minutes_ago: Option<i64>, state| SourceRow {
            source: source.to_string(),
            records,
            newest: minutes_ago.map(|m| now - Duration::minutes(m)),
            state,
        };
        let member = |name: &str, email: &str, role: &str| Member {
            name: name.to_string(),
            email: email.to_string(),
            role: role.to_string(),
        };
        Preview {
            as_of: now,
            sources: vec![
                source("github", 11, Some(4), SourceState::Live),
                source("jira", 17, Some(38), SourceState::Live),
                source(
                    "slack",
                    29,
                    Some(1),
                    SourceState::Backfilling { percent: 62 },
                ),
                source("gitlab", 0, None, SourceState::NotConnected),
            ],
            members: vec![
                member("Nadia Brooks", "nadia.brooks@kestrel.example", "owner"),
                member("Diego Alvarez", "diego.alvarez@kestrel.example", "admin"),
                member(
                    "Priya Natarajan",
                    "priya.natarajan@kestrel.example",
                    "member",
                ),
                member("Sam Whitfield", "sam.whitfield@kestrel.example", "member"),
                member("Lena Fischer", "lena.fischer@kestrel.example", "member"),
                member("Theo Park", "theo.park@kestrel.example", "member"),
            ],
            projects: [
                "github  kestrel-labs/checkout-api",
                "github  kestrel-labs/checkout-web",
                "github  kestrel-labs/platform-infra",
                "jira    CHK (Checkout)",
                "slack   #checkout-dev, #checkout-releases, #design-crit, #incidents",
            ]
            .map(String::from)
            .to_vec(),
        }
    }
}
