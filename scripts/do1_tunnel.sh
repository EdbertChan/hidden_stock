#!/usr/bin/env bash
# Open SSH tunnels for Dagster UI / Postgres / Redis on DO1.
# Usage: bash scripts/do1_tunnel.sh
# If local 5432/3000 are busy: LOCAL_UI=13000 LOCAL_PG=15432 LOCAL_REDIS=16379 bash scripts/do1_tunnel.sh
set -euo pipefail
HOST="${DO1_HOST:-157.245.231.246}"
USER="${DO1_USER:-invoker}"
KEY="${DO1_SSH_KEY:-$HOME/.ssh/id_ed25519}"
LOCAL_UI="${LOCAL_UI:-3000}"
LOCAL_PG="${LOCAL_PG:-5432}"
LOCAL_REDIS="${LOCAL_REDIS:-6379}"
echo "Dagster UI  -> http://localhost:${LOCAL_UI}"
echo "Postgres    -> localhost:${LOCAL_PG}"
echo "Redis       -> localhost:${LOCAL_REDIS}"
echo "Ctrl-C to close tunnels."
exec ssh -N \
  -L "${LOCAL_UI}:127.0.0.1:3000" \
  -L "${LOCAL_PG}:127.0.0.1:5432" \
  -L "${LOCAL_REDIS}:127.0.0.1:6379" \
  -o ServerAliveInterval=30 \
  -i "$KEY" "${USER}@${HOST}"
