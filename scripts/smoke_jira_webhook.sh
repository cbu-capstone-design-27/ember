#!/bin/sh
# Local signed-webhook smoke. Requires a throwaway JIRA_WEBHOOK_SECRET.
# Does not open a tunnel and does not call Jira.
set -eu
if [ -z "${JIRA_WEBHOOK_SECRET:-}" ]; then
  echo "Set JIRA_WEBHOOK_SECRET to a throwaway local value." >&2
  exit 2
fi
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
exec python3 "$root/services/jira-ingestion/smoke_webhook.py"
