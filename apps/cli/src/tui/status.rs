//! The Status screen: what each source has ingested, like `ember status`, plus whether it is
//! live, backfilling or not connected. Preview data for now (see `data.rs`).

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Padding, Row, Table};

use super::app::App;
use super::data::{SourceRow, SourceState};
use super::view::{PREVIEW_NOTE, ago, thousands};

pub fn draw(frame: &mut Frame, area: Rect, app: &App) {
    let preview = &app.preview;
    let block = Block::bordered()
        .padding(Padding::horizontal(1))
        .title(" Ingestion ")
        .title_bottom(Line::from(PREVIEW_NOTE).dim());
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let [table_area, _, totals_area] = Layout::vertical([
        Constraint::Length(preview.sources.len() as u16 + 1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(inner);

    let rows = preview.sources.iter().map(|row| {
        Row::new(vec![
            Line::from(row.source.clone()),
            Line::from(thousands(row.records)).right_aligned(),
            Line::from(
                row.newest
                    .map(|newest| ago(newest, preview.as_of))
                    .unwrap_or_else(|| "—".to_string()),
            ),
            state(row),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(10),
            Constraint::Length(9),
            Constraint::Length(12),
            Constraint::Min(0),
        ],
    )
    .header(Row::new(["Source", "  Records", "Newest", "State"]).bold())
    .column_spacing(2);
    frame.render_widget(table, table_area);

    let connected: Vec<&SourceRow> = preview
        .sources
        .iter()
        .filter(|row| row.state != SourceState::NotConnected)
        .collect();
    let total: u64 = connected.iter().map(|row| row.records).sum();
    frame.render_widget(
        Line::from(format!(
            "{} records across {} connected sources",
            thousands(total),
            connected.len()
        ))
        .dim(),
        totals_area,
    );
}

fn state(row: &SourceRow) -> Line<'static> {
    let (text, style) = match row.state {
        SourceState::Live => ("● live".to_string(), Style::new().fg(Color::Green)),
        SourceState::Backfilling { percent } => (
            format!("◐ backfilling {percent}%"),
            Style::new().fg(Color::Yellow),
        ),
        SourceState::NotConnected => ("○ not connected".to_string(), Style::new().dim()),
    };
    Line::from(Span::styled(text, style))
}

#[cfg(test)]
mod tests {
    use super::super::app::Screen;
    use super::super::data::Account;
    use super::super::view::tests::{app_on, screen};

    #[test]
    fn lists_every_source_with_its_count_age_and_state() {
        let text = screen(&app_on(Screen::Status, Account::Checking), 100, 24);
        for expected in [
            "Ingestion",
            "Source",
            "Records",
            "Newest",
            "State",
            "github",
            "4 min ago",
            "● live",
            "slack",
            "◐ backfilling 62%",
            "gitlab",
            "○ not connected",
        ] {
            assert!(text.contains(expected), "missing {expected:?} in:\n{text}");
        }
    }

    #[test]
    fn totals_count_only_connected_sources() {
        let text = screen(&app_on(Screen::Status, Account::Checking), 100, 24);
        // github 11 + jira 17 + slack 29; gitlab isn't connected
        assert!(
            text.contains("57 records across 3 connected sources"),
            "{text}"
        );
    }

    #[test]
    fn says_the_numbers_are_preview_data() {
        let text = screen(&app_on(Screen::Status, Account::Checking), 100, 24);
        assert!(
            text.contains("preview data until the Ember API lands"),
            "{text}"
        );
    }
}
