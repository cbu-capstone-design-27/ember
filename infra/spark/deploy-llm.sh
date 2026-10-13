#!/usr/bin/env bash
# Deploy or refresh the chat model (vLLM) on the DGX Spark (EMBER-49, ADR 0005).
# Usage, from the Spark or via `ssh shared-dev ember/infra/spark/deploy-llm.sh [git-ref]`:
#   infra/spark/deploy-llm.sh             # fast-forward the current branch
#   infra/spark/deploy-llm.sh <branch>    # switch to origin/<branch>
# Optional env: LLM_BIND_ADDR (written to .env), DEPLOY_SKIP_GIT=1 to deploy the checkout as it is.
# The API key lives only in .env. Bind to the Tailscale address once the Spark has one; until then
# the default is loopback, which only an SSH tunnel can reach.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ "${DEPLOY_SKIP_GIT:-}" != "1" ]; then
  if [ -n "${1:-}" ]; then
    git fetch -q origin "$1"
    git checkout -q -B "$1" "origin/$1"
  else
    git pull -q --ff-only
  fi
fi
git log --oneline -1

[ -f .env ] || cp .env.example .env
chmod 600 .env

set_env() {
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
get_env() { grep "^$1=" .env | head -1 | cut -d= -f2- || true; }

grep -Eq '^LLM_API_KEY=.+' .env || set_env LLM_API_KEY "$(openssl rand -hex 32)"
[ -z "${LLM_BIND_ADDR:-}" ] || set_env LLM_BIND_ADDR "$LLM_BIND_ADDR"

docker compose -f infra/docker-compose.yml --env-file .env --profile llm \
  up -d --force-recreate llm

addr=$(get_env LLM_BIND_ADDR); port=$(get_env LLM_PORT)
url="http://${addr:-127.0.0.1}:${port:-8001}"
echo "waiting for $url (the first start downloads the model and can take many minutes)"
ready=0
for _ in $(seq 1 360); do
  if curl -s -o /dev/null -f "$url/health"; then ready=1; break; fi
  sleep 5
done
[ "$ready" = "1" ] || { echo "llm did not become healthy; see: docker logs ember-llm"; exit 1; }

key=$(get_env LLM_API_KEY)
model=$(get_env LLM_SERVED_NAME); model=${model:-qwen3-30b-a3b-instruct}
echo "no key   -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "$url/v1/models")  (expect 401)"
reply=$(curl -s "$url/v1/chat/completions" -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$model\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: pong\"}]}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip())')
echo "with key -> reply: $reply  (expect pong)"
[ -n "$reply" ]
