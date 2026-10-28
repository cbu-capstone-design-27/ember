<p align="center">
  <img src=".github/assets/ember-logo.png" alt="Ember logo: a flame with graph nodes" width="120">
</p>

<h1 align="center">Ember</h1>

<p align="center">
  <b>Shared project context for AI coding agents, served over MCP.</b><br>
  A capstone project at California Baptist University, Fall 2026 to Spring 2027.
</p>

<p align="center">
  <a href="#quick-start"><b>Quick start</b></a>
  &nbsp;·&nbsp;
  <a href="docs/ingestion/architecture.md">Architecture</a>
  &nbsp;·&nbsp;
  <a href="docs/adr">Decisions</a>
  &nbsp;·&nbsp;
  <a href="https://ember-capstone.atlassian.net/browse/EMBER">Jira board</a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0 license"></a>
</p>

Coding agents start every session knowing only the code and whatever is written in files like `AGENTS.md`. The reasons behind the code live in pull requests, tickets and chat threads, and no static file keeps up with them.

Ember collects that record from GitHub, GitLab, Jira and Slack as it happens. It stores each pull request, ticket and message in Postgres, embeds it with one shared model, and serves search over it through an MCP server, so Claude Code, Codex, Cursor or any other MCP client can ask what was decided and why. The web dashboard also shows where the sources disagree, such as a ticket still in progress after its pull request merged.

## How it works

```mermaid
flowchart LR
  S["GitHub, GitLab,<br/>Jira, Slack"] -->|webhooks| I["intake<br/>verify, store raw_events"]
  I --> E["enrich<br/>one worker per source"]
  E --> SR[("source_records<br/>+ pgvector embeddings")]
  SR --> M["MCP server<br/>search"]
  M --> A["Claude Code, Codex,<br/>Cursor"]
```

Intake is the one webhook listener for every source. It verifies each delivery, stores it as it arrived and answers right away. A worker per source then enriches the delivery, fetching only what the payload lacks, and writes a `source_records` row. The embed worker adds a vector to each row, and the MCP server searches those vectors. Postgres holds every hand-off, so each stage can be stopped, replayed or replaced on its own. Extracting a typed knowledge graph into Neo4j is the next stage.

The full design is in [`docs/ingestion/architecture.md`](docs/ingestion/architecture.md) and [ADR 0004](docs/adr/0004-ingestion-architecture.md).

## Repository layout

| Path | What is in it |
| --- | --- |
| [`apps/`](apps) | The [web app](apps/web) (Next.js, Better Auth), the [MCP server](apps/mcp) and the [CLI](apps/cli) |
| [`services/`](services) | [intake](services/intake), the per-source ingestion and enrich workers, the [embed](services/embed) worker and the [embedding service](services/embedding) |
| [`packages/`](packages) | Shared libraries, added only once a second consumer needs them |
| [`infra/`](infra) | Local Docker Compose, and the homelab k3s cluster: Ansible for the nodes, Flux deploying [`infra/k8s`](infra/k8s) from `develop` |
| [`docs/`](docs) | [Architecture decisions](docs/adr), the [ingestion design](docs/ingestion), the [graph schema](docs/graph-schema.md) and the [working agreement](docs/working-agreement.md) |

## Quick start

You need Docker and Node 22 or later. From the repository root:

```sh
cp .env.example .env

# Postgres 18 with pgvector, the version the cluster runs
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres up -d --wait postgres

# The web app at http://localhost:3000
cd apps/web
npm ci
DATABASE_URL=postgres://ember:change-me-locally@localhost:5432/ember \
  BETTER_AUTH_SECRET=dev-secret-change-me BETTER_AUTH_URL=http://localhost:3000 npm run dev
```

Each component's README covers running it and its tests:

- [`apps/web`](apps/web/README.md): the dashboard, the graph view and CLI sign-in
- [`services/intake`](services/intake/README.md): receiving webhooks locally (Compose profile `intake`)
- [`services/embed`](services/embed/README.md): embedding `source_records`
- [`apps/mcp`](apps/mcp/README.md): the MCP server
- [`apps/cli`](apps/cli/README.md): the `ember` command, starting with `ember status`

## Status

Ember is in active development and is not ready for outside use. Intake, the enrich workers for GitHub, GitLab, Jira and Slack, and the embed worker are merged. Intake and the GitHub enrich worker run on the team's cluster. The [MCP server](https://github.com/cbu-capstone-design-27/ember/pull/52) and [vector search](https://github.com/cbu-capstone-design-27/ember/pull/64) are in open pull requests, and the web dashboard runs on preview data.

Launch scope is tracked on the [EMBER](https://ember-capstone.atlassian.net/browse/EMBER) Jira board by label:

- [Floor](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20floor): guaranteed for April 2027
- [Drop-first](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20drop-first): targeted, cut first under pressure
- [Not at launch](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20not-at-launch): explicitly out of April 2027 scope
- [Coursework](https://ember-capstone.atlassian.net/issues/?jql=project%20%3D%20EMBER%20AND%20labels%20%3D%20coursework): capstone deliverables (EMBER-26)

## Contributing

Every change starts from a Jira ticket and lands as a pull request into `develop`. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the branch and PR conventions, and [`AGENTS.md`](AGENTS.md) has the rules every ingestion worker follows. Report security issues as described in [`SECURITY.md`](SECURITY.md).

## Team

Ryan Stoffel, Payton Henry, Elijah Tabor, Brandon Magana and Jacob Pugh, for the CBU senior capstone.

## License

[Apache 2.0](LICENSE)
