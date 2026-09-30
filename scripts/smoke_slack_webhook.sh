#!/bin/sh
# Local signed-webhook smoke. Requires a throwaway SLACK_SIGNING_SECRET.
# Does not open a tunnel and does not call Slack.
set -eu
if [ -z "${SLACK_SIGNING_SECRET:-}" ]; then
  echo "Set SLACK_SIGNING_SECRET to a throwaway local value." >&2
  exit 2
fi
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
exec python3 "$root/services/slack-ingestion/smoke_webhook.py"
