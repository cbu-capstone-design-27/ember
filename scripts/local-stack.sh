#!/usr/bin/env bash
# EMBER-166: run Ember on this Mac against the compose Postgres, never the cluster's. See apps/mcp/README.md,
# "Local stack". From the repo root:
#
#   scripts/local-stack.sh
#
# 1. Starts the compose Postgres (pgvector, the cluster's version), applies the migrations and creates
#    the MCP's login role ember_mcp.
# 2. Runs, in the foreground until Ctrl+C, with logs in $LOG_DIR (default /tmp/ember-local-stack):
#    - the web app on that Postgres, on http://localhost:3000, so accounts and memberships are shared
#    - ember-backend (services/backend): intake on :8080 and every enrich and backfill part whose settings
#      are in .env. It disables the others and says why (`python -m ember check`)
#    - extract, load with GRAPH_STORE=postgres, and embed when EMBEDDING_URL is set
#
# Settings come from .env (or ENV_FILE). DATABASE_URL in it is ignored: everything here uses the
# compose database.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=${ENV_FILE:-.env}
[ -f "$ENV_FILE" ] || ENV_FILE=.env.example
set -a
# shellcheck disable=SC1090
. "./$ENV_FILE"
set +a
export DATABASE_URL="postgresql://ember:${POSTGRES_PASSWORD:-change-me-locally}@localhost:${POSTGRES_PORT:-5432}/ember"
LOG_DIR=${LOG_DIR:-/tmp/ember-local-stack}
mkdir -p "$LOG_DIR"

compose() { docker compose -f infra/docker-compose.yml --env-file "$ENV_FILE" --profile postgres "$@"; }
compose up -d --wait postgres
compose run --rm postgres-migrate
compose run --rm postgres-mcp-role

PY=.venv/bin/python
[ -x "$PY" ] || python3.12 -m venv .venv
"$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip >/dev/null   # a venv made by uv has no pip
"$PY" -m pip install -q -r apps/mcp/requirements.txt -r services/embed/requirements.txt \
  -r services/pipeline/requirements.txt ./services/backend
[ -d apps/web/node_modules ] || (cd apps/web && npm ci --no-audit --no-fund)

pids=()
start() {  # start NAME COMMAND...: run in the background, output in $LOG_DIR/NAME.log
  local name=$1; shift
  "$@" >"$LOG_DIR/$name.log" 2>&1 &
  pids+=($!)
  echo "started $name (log: $LOG_DIR/$name.log)"
}
skip() { echo "skipped $1: set $2 in $ENV_FILE"; }
trap 'kill ${pids[@]+"${pids[@]}"} 2>/dev/null; wait; echo stopped' INT TERM EXIT

start web env -C apps/web BETTER_AUTH_SECRET="${BETTER_AUTH_SECRET:-dev-secret-change-me}" \
  BETTER_AUTH_URL="${BETTER_AUTH_URL:-http://localhost:3000}" npm run dev

"$PY" -m ember check || true   # which intake, enrich and backfill parts run, and why the others don't
start backend "$PY" -m ember run

start extract env -C services/pipeline "../../$PY" -m loader extract
start load env -C services/pipeline GRAPH_STORE=postgres "../../$PY" -m loader load
if [ -n "${EMBEDDING_URL:-}" ]; then
  start embed "$PY" services/embed/embed.py
else skip embed "EMBEDDING_URL (search then runs on BM25 and graph traversal)"; fi

echo "Ember is running on $DATABASE_URL. Ctrl+C stops everything."
wait
