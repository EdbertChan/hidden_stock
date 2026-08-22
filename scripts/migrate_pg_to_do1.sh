#!/usr/bin/env bash
# Dump local Compose Postgres and restore onto DO1 Dagster Postgres.
# Usage: bash scripts/migrate_pg_to_do1.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${DO1_SSH_KEY:-$HOME/.ssh/id_ed25519}"
HOST="${DO1_HOST:-157.245.231.246}"
USER="${DO1_USER:-invoker}"
REMOTE_DIR="${DO1_REMOTE_DIR:-/home/invoker/hidden_stock}"
DUMP="/tmp/hidden_stock_dagster_$(date +%Y%m%d%H%M%S).dump"

echo "==> Dumping local dagster DB (binary-safe)"
if docker ps --format '{{.Names}}' | grep -q 'postgres'; then
  LOCAL_PG=$(docker ps --format '{{.Names}}' | grep postgres | head -1)
  docker exec "$LOCAL_PG" pg_dump -U dagster -Fc -f /tmp/hs_migrate.dump dagster
  docker cp "$LOCAL_PG:/tmp/hs_migrate.dump" "$DUMP"
else
  PGPASSWORD="${POSTGRES_PASSWORD:-dagster}" pg_dump -h localhost -U dagster -Fc -f "$DUMP" dagster
fi
ls -lh "$DUMP"

echo "==> Copy dump to DO1"
scp -i "$KEY" -o ConnectTimeout=20 "$DUMP" "${USER}@${HOST}:/tmp/hs.dump"

echo "==> Restore into Compose postgres on DO1"
ssh -i "$KEY" -o ConnectTimeout=20 -o ServerAliveInterval=10 "${USER}@${HOST}" bash -s <<EOF
set -euo pipefail
cd ${REMOTE_DIR}
docker compose stop dagster_webserver dagster_daemon hidden_stock_code || true
docker compose cp /tmp/hs.dump postgres:/tmp/hs.dump
docker compose exec -T postgres psql -U dagster -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='dagster' AND pid <> pg_backend_pid();" || true
docker compose exec -T postgres dropdb -U dagster --if-exists dagster
docker compose exec -T postgres createdb -U dagster dagster
docker compose exec -T postgres pg_restore -U dagster -d dagster --no-owner /tmp/hs.dump
docker compose exec -T postgres psql -U dagster -d dagster -c "\\dn"
docker compose exec -T postgres psql -U dagster -d dagster -c "SELECT count(*) AS backtest_summary_rows FROM stock_data.backtest_summary;"
docker compose up -d
EOF

echo "DONE migrate"
rm -f "$DUMP"
