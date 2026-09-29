#!/bin/sh
# Local signed-webhook smoke. Requires a throwaway GITHUB_WEBHOOK_SECRET.
# Does not open a tunnel and does not call GitHub.
set -eu
if [ -z "${GITHUB_WEBHOOK_SECRET:-}" ]; then
  echo "Set GITHUB_WEBHOOK_SECRET to a throwaway local value." >&2
  exit 2
fi
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
exec python3 "$root/services/github-ingestion/smoke_webhook.py"
