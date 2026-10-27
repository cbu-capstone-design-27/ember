//! `ember status` (EMBER-82): what ingestion data is in Postgres. For each source, how many
//! `source_records` rows there are and the newest `occurred_at`, the source's own time.

use chrono::{DateTime, SecondsFormat, Utc};
use sqlx::Connection;

use crate::{Error, db};

/// The launch sources, listed in this order whether or not they have records. A source outside
/// this list that has records is listed after them.
const SOURCES: [&str; 4] = ["github", "jira", "slack", "gitlab"];

const QUERY: &str = "select source, count(*), max(occurred_at) from source_records group by source";

/// Postgres's code for "relation does not exist".
const UNDEFINED_TABLE: &str = "42P01";

#[derive(Debug, PartialEq)]
struct SourceCount {
    source: String,
    records: i64,
    newest: Option<DateTime<Utc>>,
}

pub async fn run() -> Result<(), Error> {
    let options = db::options_from_env()?;
    let mut connection = db::connect(&options).await?;
    let rows: Vec<(String, i64, Option<DateTime<Utc>>)> = sqlx::query_as(QUERY)
        .fetch_all(&mut connection)
        .await
        .map_err(|error| query_failed(&error, &db::describe(&options)))?;
    // Closing only says goodbye to the server; the counts are already in hand
    let _ = connection.close().await;
    print!("{}", render(&tally(rows)));
    Ok(())
}

fn query_failed(error: &sqlx::Error, target: &str) -> Error {
    if let sqlx::Error::Database(db_error) = error
        && db_error.code().as_deref() == Some(UNDEFINED_TABLE)
    {
        return Error::Database(format!(
            "{target} has no source_records table. Apply the ingestion migrations \
             (infra/k8s/apps/postgres/migrations) to it with dbmate."
        ));
    }
    Error::Database(format!(
        "reading source_records from {target} failed: {}",
        db::reason(error)
    ))
}

/// Puts the query's rows in display order and fills in the launch sources that have no rows.
fn tally(rows: Vec<(String, i64, Option<DateTime<Utc>>)>) -> Vec<SourceCount> {
    let mut found: Vec<SourceCount> = rows
        .into_iter()
        .map(|(source, records, newest)| SourceCount {
            source,
            records,
            newest,
        })
        .collect();
    let mut counts: Vec<SourceCount> = SOURCES
        .iter()
        .map(
            |&source| match found.iter().position(|count| count.source == source) {
                Some(index) => found.remove(index),
                None => SourceCount {
                    source: source.to_string(),
                    records: 0,
                    newest: None,
                },
            },
        )
        .collect();
    found.sort_by(|a, b| a.source.cmp(&b.source));
    counts.extend(found);
    counts
}

/// The table `ember status` prints. Times are UTC, to the second.
fn render(counts: &[SourceCount]) -> String {
    let newest = |count: &SourceCount| match count.newest {
        Some(time) => time.to_rfc3339_opts(SecondsFormat::Secs, true),
        None => "-".to_string(),
    };
    let source_width = counts
        .iter()
        .map(|c| c.source.len())
        .chain(["SOURCE".len()])
        .max()
        .unwrap_or(0);
    let records_width = counts
        .iter()
        .map(|c| c.records.to_string().len())
        .chain(["RECORDS".len()])
        .max()
        .unwrap_or(0);

    let mut out = format!(
        "{:<source_width$}  {:>records_width$}  NEWEST\n",
        "SOURCE", "RECORDS"
    );
    for count in counts {
        out += &format!(
            "{:<source_width$}  {:>records_width$}  {}\n",
            count.source,
            count.records,
            newest(count)
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(rfc3339: &str) -> Option<DateTime<Utc>> {
        Some(rfc3339.parse().unwrap())
    }

    #[test]
    fn launch_sources_come_first_and_always_appear() {
        let rows = vec![
            ("teams".to_string(), 1, at("2026-10-04T12:00:00Z")),
            ("gitlab".to_string(), 3, at("2026-10-03T09:15:42Z")),
            ("github".to_string(), 2, at("2026-10-02T10:00:00Z")),
        ];
        let sources: Vec<String> = tally(rows).into_iter().map(|c| c.source).collect();
        assert_eq!(sources, ["github", "jira", "slack", "gitlab", "teams"]);
    }

    #[test]
    fn renders_an_aligned_table() {
        let counts = vec![
            SourceCount {
                source: "github".into(),
                records: 12345678,
                newest: at("2026-10-02T10:00:00.999Z"),
            },
            SourceCount {
                source: "jira".into(),
                records: 0,
                newest: None,
            },
        ];
        assert_eq!(
            render(&counts),
            "SOURCE   RECORDS  NEWEST\n\
             github  12345678  2026-10-02T10:00:00Z\n\
             jira           0  -\n"
        );
    }
}
