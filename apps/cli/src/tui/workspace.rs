//! The Workspace screen: who you're signed in as and the organization you work in (real, from
//! the web app), then its members and what it ingests (preview data for now, see `data.rs`).

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Stylize;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Padding, Paragraph, Row, Table, Wrap};

use super::app::App;
use super::data::Account;
use super::view::{PREVIEW_NOTE, clean};

pub fn draw(frame: &mut Frame, area: Rect, app: &App) {
    let members = &app.preview.members;
    let [account_area, members_area, projects_area] = Layout::vertical([
        Constraint::Length(7),
        Constraint::Length(members.len() as u16 + 3),
        Constraint::Min(0),
    ])
    .areas(area);

    frame.render_widget(
        Paragraph::new(account_lines(&app.account))
            .wrap(Wrap { trim: false })
            .block(
                Block::bordered()
                    .padding(Padding::horizontal(1))
                    .title(" Account "),
            ),
        account_area,
    );

    let rows = members.iter().map(|member| {
        Row::new(vec![
            member.name.clone(),
            member.email.clone(),
            member.role.clone(),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(18),
            Constraint::Length(32),
            Constraint::Min(0),
        ],
    )
    .header(Row::new(["Name", "Email", "Role"]).bold())
    .column_spacing(2)
    .block(preview_block(" Members "));
    frame.render_widget(table, members_area);

    let projects: Vec<Line> = app
        .preview
        .projects
        .iter()
        .map(|p| Line::from(p.as_str()))
        .collect();
    frame.render_widget(
        Paragraph::new(projects).block(preview_block(" Ingested ")),
        projects_area,
    );
}

fn preview_block(title: &str) -> Block<'_> {
    Block::bordered()
        .padding(Padding::horizontal(1))
        .title(title)
        .title_bottom(Line::from(PREVIEW_NOTE).dim())
}

/// The sign-in, as `ember auth status` reports it.
fn account_lines(account: &Account) -> Vec<Line<'static>> {
    let label = |text: &str| Span::from(format!("{text:<14}")).dim();
    match account {
        Account::Checking => vec![Line::from("Checking your sign-in…").dim()],
        Account::NotSignedIn => vec![
            Line::from("Not logged in."),
            Line::from("Run `ember auth login` to see your account and organization here."),
        ],
        Account::Unavailable(problem) => vec![
            Line::from(clean(problem)).yellow(),
            Line::from("The rest of this screen is preview data.").dim(),
        ],
        Account::SignedIn(me) => {
            let mut lines = vec![
                Line::from(vec![
                    label("Signed in as"),
                    Span::from(clean(&me.email)).bold(),
                ]),
                Line::from(vec![label("Web app"), Span::from(clean(&me.url))]),
            ];
            match (&me.organization, me.organizations) {
                (Some(org), count) => {
                    let role = org.role.as_deref().map(clean).unwrap_or_default();
                    lines.push(Line::from(vec![
                        label("Organization"),
                        Span::from(clean(&org.name)).bold(),
                        Span::from(format!(" ({})", clean(&org.slug))),
                        Span::from(if role.is_empty() {
                            String::new()
                        } else {
                            format!("  ·  your role: {role}")
                        }),
                    ]));
                    if let Some(tenant) = &org.tenant {
                        lines.push(Line::from(vec![label("Tenant"), Span::from(clean(tenant))]));
                    }
                    if let Some(others @ 1..) = count.map(|n| n.saturating_sub(1)) {
                        let noun = if others == 1 {
                            "organization"
                        } else {
                            "organizations"
                        };
                        lines.push(Line::from(vec![
                            label(""),
                            Span::from(format!(
                                "+{others} other {noun}. The CLI works in the first one you joined."
                            ))
                            .dim(),
                        ]));
                    }
                }
                (None, Some(_)) => lines.push(Line::from(vec![
                    label("Organization"),
                    Span::from(format!(
                        "none yet. Create or join one at {}/organizations",
                        clean(&me.url)
                    ))
                    .yellow(),
                ])),
                (None, None) => {} // the web app predates organizations
            }
            lines
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::app::Screen;
    use super::super::data::Account;
    use super::super::view::tests::{ada, app_on, screen};
    use crate::auth::SignedIn;

    fn workspace(account: Account) -> String {
        screen(&app_on(Screen::Workspace, account), 100, 30)
    }

    #[test]
    fn signed_in_shows_the_real_account_and_active_organization() {
        let text = workspace(ada());
        for expected in [
            "Signed in as  ada@example.com",
            "Web app       https://ember.example",
            "Organization  Kestrel Labs (kestrel-labs)  ·  your role: owner",
            "Tenant        ember-development",
            "+1 other organization",
        ] {
            assert!(text.contains(expected), "missing {expected:?} in:\n{text}");
        }
    }

    #[test]
    fn signed_in_without_an_organization_says_where_to_get_one() {
        let text = workspace(Account::SignedIn(SignedIn {
            url: "https://ember.example".into(),
            email: "ada@example.com".into(),
            organization: None,
            organizations: Some(0),
        }));
        assert!(
            text.contains("none yet. Create or join one at https://ember.example/organizations"),
            "{text}"
        );
    }

    #[test]
    fn not_signed_in_says_how_to_sign_in() {
        let text = workspace(Account::NotSignedIn);
        assert!(text.contains("Not logged in."), "{text}");
        assert!(text.contains("Run `ember auth login`"), "{text}");
    }

    #[test]
    fn a_sign_in_problem_is_shown_as_the_web_app_worded_it() {
        let text = workspace(Account::Unavailable(
            "Cannot reach the web app at https://ember.example: connection refused".into(),
        ));
        assert!(
            text.contains("Cannot reach the web app at https://ember.example"),
            "{text}"
        );
    }

    #[test]
    fn members_and_projects_are_labelled_preview_data() {
        let text = workspace(ada());
        assert!(
            text.contains("Nadia Brooks") && text.contains("owner"),
            "{text}"
        );
        assert!(text.contains("kestrel-labs/checkout-api"), "{text}");
        assert_eq!(
            text.matches("preview data until the Ember API lands")
                .count(),
            2,
            "{text}"
        );
    }
}
