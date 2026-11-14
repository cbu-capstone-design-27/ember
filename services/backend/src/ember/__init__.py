"""ember-backend (EMBER-160, ADR 0012): intake, the enrich workers and the scheduled jobs in one process.

Layout:

- `runtime`: the parts every component shares. `Component` is what the controller starts, supervises
  and stops; `Supervisor` restarts one that crashes without touching the others; `Controller` builds
  everything from settings and handles startup and shutdown.
- `core`: settings, Postgres, the shared models, errors and source credentials.
- `intake`: the one webhook listener (EMBER-62), serving the adapter of every configured source.
- `work`: the base classes for workers that claim rows from Postgres.
- `connectors`: one package per source. A connector builds that source's parts from its settings.
"""
