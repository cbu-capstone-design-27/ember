# Privacy policy

- Last updated: 2026-11-04
- Jira: EMBER-131

Ember is a student capstone project at California Baptist University (the `cbu-capstone-design-27` team). It builds a knowledge graph of a team's work from the tools the team connects to it: GitHub, Jira, Slack and GitLab. This page says what Ember collects, where it is kept, and how to have it removed. It covers the Ember web app, its ingestion services, and the Atlassian, GitHub and Slack apps an organization connects through it.

## What Ember collects

Ember reads only from the accounts an organization's owner or admin connects, and only what that connection allows:

- **Jira:** issues in the projects the owner picks, with their comments, change history, worklogs, attachments' names and metadata (never the files), links, watchers and votes. This includes the names, Atlassian account ids and avatars of the people on them. Ember reads Jira as the Atlassian user who consented, so it sees only what that user can see.
- **GitHub:** pull requests, reviews, issues and comments in the repositories the `ember-ingest` App is installed on.
- **Slack:** messages and threads in the channels Ember is invited to.
- **GitLab:** merge requests, issues and notes in the group whose token the owner enters.
- **Ember accounts:** the name, email address and hashed password of each person who signs up, and the organizations they belong to.

To keep reading a source, Ember stores the credentials the connection issues: an access and refresh token, a bot token, or an access token. They are encrypted (AES-256-GCM), and the web app never shows them again.

## How it is used

Ember turns what it reads into a knowledge graph and a search index, so the organization's members can ask questions about their own work. Each organization's data is kept under its own tenant. Members of one organization can't see another's.

Ember doesn't sell data, show advertising, or share data with anyone outside the project, except as listed below.

## Where it is kept and processed

- **Storage:** Postgres and Neo4j on the team's own Kubernetes cluster.
- **Language models:** text is embedded and summarized by models the team runs on a university DGX Spark. Hosted model APIs aren't used on the cluster.
- **Monitoring:** the cluster sends metrics and service logs to Grafana Cloud. The ingestion services never write what they ingest into their logs.
- **Code hosting:** the source code and container images are public on GitHub. No customer data is stored there.

## How long it is kept, and removing it

Ember keeps an organization's ingested data until the organization asks for it to be deleted.

- **Disconnecting** a source in Settings stops ingesting it at once and deletes its stored credentials. For Jira, Ember also deletes the webhook it registered. The data already ingested isn't deleted automatically yet.
- **Revoking access** on the source's side also stops ingestion. For Atlassian, the user who consented can remove Ember from their account's connected apps.
- **To have an organization's data, or your own, deleted,** contact the project through the repository on GitHub (see [`SECURITY.md`](SECURITY.md)). The team deletes it from Postgres and the graph.

## Security

Credentials are encrypted at rest with keys kept apart from the data. Every webhook delivery is verified before it is stored. Access to the cluster is limited to the project team. Report a security problem as [`SECURITY.md`](SECURITY.md) describes.

## Changes and contact

Changes to this policy are made in this file, and its history shows them. Questions go to the project through its GitHub repository: <https://github.com/cbu-capstone-design-27/ember>.
