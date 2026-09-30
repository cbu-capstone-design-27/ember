#!/usr/bin/env bash
# Deploy or refresh the embedding service on the DGX Spark (EMBER-33).
# Usage, from the Spark or via `ssh shared-dev ember/infra/spark/deploy.sh [git-ref]`:
#   infra/spark/deploy.sh                 # fast-forward the current branch
#   infra/spark/deploy.sh <branch>        # switch to origin/<branch>
# Optional env: EMBEDDING_BIND_ADDR (written to .env). The API key lives only in .env.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -n "${1:-}" ]; then
  git fetch -q origin "$1"
  git checkout -q -B "$1" "origin/$1"
else
  git pull -q --ff-only
fi
git log --oneline -1

[ -f .env ] || cp .env.example .env
chmod 600 .env

set_env() {
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
get_env() { grep "^$1=" .env | head -1 | cut -d= -f2-; }

grep -Eq '^EMBEDDING_API_KEY=.+' .env || set_env EMBEDDING_API_KEY "$(openssl rand -hex 32)"
[ -z "${EMBEDDING_BIND_ADDR:-}" ] || set_env EMBEDDING_BIND_ADDR "$EMBEDDING_BIND_ADDR"

docker compose -f infra/docker-compose.yml --env-file .env --profile embedding \
  up -d --force-recreate embedding

addr=$(get_env EMBEDDING_BIND_ADDR); port=$(get_env EMBEDDING_PORT)
url="http://${addr:-127.0.0.1}:${port:-8000}"
for _ in $(seq 1 60); do
  curl -s -o /dev/null "$url/health" && break
  sleep 5
done

key=$(get_env EMBEDDING_API_KEY)
echo "no key   -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "$url/v1/models")  (expect 401)"
dim=$(curl -s "$url/v1/embeddings" -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding-0.6b","input":["deploy check"]}' \
  | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["data"][0]["embedding"]))')
echo "with key -> $dim dimensions  (expect 1024)"
[ "$dim" = "1024" ]
