#!/bin/sh
# Local signed-webhook smoke. Requires a throwaway GITLAB_WEBHOOK_SIGNING_TOKEN
# (whsec_ plus base64). Does not open a tunnel and does not call GitLab.
set -eu
if [ -z "${GITLAB_WEBHOOK_SIGNING_TOKEN:-}" ]; then
  echo "Set GITLAB_WEBHOOK_SIGNING_TOKEN to a throwaway whsec_<base64> value." >&2
  exit 2
fi
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
exec python3 "$root/services/gitlab-ingestion/smoke_webhook.py"
