# ember

A graph-based contextual retrieval system for AI coding agents.

ember replaces static context files like AGENTS.md with a typed knowledge graph of a project's decisions, conventions, people, tickets, and modules. The graph updates automatically from GitHub, Jira, and Slack activity and is exposed to any AI coding tool through an MCP server.

## Status

Early development. Capstone project, Fall 2026 - Spring 2027.

## Scope

Launch scope is tracked on the [EMBER](https://ember-capstone.atlassian.net/browse/EMBER) Jira board by label. Labels document existing board taxonomy only; they are not a feature checklist in this README.

- [Floor](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20floor) - guaranteed for April 2027
- [Drop-first](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20drop-first) - targeted, cut first under pressure
- [Not at launch](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20not-at-launch) - explicitly out of April 2027 scope
- [Coursework](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20coursework) - capstone deliverables (EMBER-26)

## Repository layout

Monorepo. Each top-level area is a stub until its own ticket lands code.

```
apps/
  web/          Web frontend
  cli/          Command-line interface
  mcp/          MCP server exposing the graph to AI coding tools
services/
  pipeline/          Ingestion pipeline and job queue (GitHub, Jira, Slack -> Neo4j)
  github-ingestion/  GitHub webhook receiver; emits {type, body} (EMBER-35)
packages/            Shared libraries - add one only when there is a second consumer
infra/               Local infrastructure (docker-compose: Neo4j; optional github-ingestion profile)
docs/
  adr/          Architecture Decision Records (see 0000-use-adrs.md)
  working-agreement.md
.github/        PR template, CODEOWNERS, CI (`ci-ok` required check), Dependabot
```

See `CONTRIBUTING.md`, `SECURITY.md`, and `.env.example` at the root.

## Branches

- `main` - release branch. Only receives merges from `develop`.
- `develop` - integration branch. All feature PRs target this.
- `feature/EMBER-<n>-<short-slug>` - one branch per Jira ticket, branched from `develop`.
- `hotfix/EMBER-<n>-<short-slug>` - branched from `main`, merged back to both `main` and `develop`.

Every PR title starts with its Jira key and follows `.github/PULL_REQUEST_TEMPLATE.md`. The single required CI check is `ci-ok`.

## Team

Ryan Stoffel, Payton Henry, Elijah Tabor, Brandon Magana, Jacob Pugh

## License

Apache 2.0
