# packages

Shared libraries consumed by more than one app or service.

## Rule: add a package only when there is a second consumer

Do not pre-emptively extract code into `packages/`. Code starts in the app or service that needs it. When a *second* app or service needs the same thing, extract it into a package here and point both at it.

Reasons:

- Avoids speculative abstractions that never get a second user.
- Keeps each app/service self-contained while the design is still moving.
- Makes the extraction a deliberate, reviewable step with a real consumer on each side.

## Current packages

- `ingestion-envelope` — v1 shared ingestion intake, `{type, body}` raw connector JSON (EMBER-39). Landed as a package because every source connector and the pipeline share it, before those consumers have their own code.

## Adding a package

1. Name the directory after what it provides (e.g. `packages/graph-client`), not after who uses it.
2. Give it its own `README.md` stating its consumers.
3. Add a `CODEOWNERS` line for it in `.github/CODEOWNERS`.
