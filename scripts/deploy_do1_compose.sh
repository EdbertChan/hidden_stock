#!/usr/bin/env bash
# Deploy hidden_stock Dagster Compose stack to DO1 (alongside Invoker).
# Usage: bash scripts/deploy_do1_compose.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${DO1_HOST:-157.245.231.246}"
USER="${DO1_USER:-invoker}"
KEY="${DO1_SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${DO1_REMOTE_DIR:-/home/invoker/hidden_stock}"
SSH=(ssh -o ConnectTimeout=20 -o ServerAliveInterval=10 -i "$KEY" "${USER}@${HOST}")
RSYNC=(rsync -az --delete
  --exclude .venv
  --exclude .git
  --exclude .cache
  --exclude __pycache__
  --exclude '*.pyc'
  --exclude .telemetry
  --exclude hidden_stock.egg-info
  --exclude .dagster
  -e "ssh -i $KEY -o ConnectTimeout=20")

echo "==> Syncing repo to ${USER}@${HOST}:${REMOTE_DIR}"
"${RSYNC[@]}" "$ROOT/" "${USER}@${HOST}:${REMOTE_DIR}/"

echo "==> Ensuring .env on remote"
"${SSH[@]}" "test -f ${REMOTE_DIR}/.env || cp ${REMOTE_DIR}/.env.example ${REMOTE_DIR}/.env"

# Copy local .env if present (API keys)
if [[ -f "$ROOT/.env" ]]; then
  scp -i "$KEY" -o ConnectTimeout=20 "$ROOT/.env" "${USER}@${HOST}:${REMOTE_DIR}/.env"
fi

echo "==> docker compose up --build -d"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose up -d --build"

echo "==> Waiting for health"
"${SSH[@]}" "cd ${REMOTE_DIR} && for i in \$(seq 1 60); do
  docker compose ps
  if docker compose exec -T postgres pg_isready -U dagster >/dev/null 2>&1; then
    echo postgres_ready
    break
  fi
  sleep 5
done"

"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose ps"
echo "DONE. Tunnel: ssh -L 3000:127.0.0.1:3000 -L 5432:127.0.0.1:5432 -L 6379:127.0.0.1:6379 -i $KEY ${USER}@${HOST}"
