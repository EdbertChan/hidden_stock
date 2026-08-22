#!/usr/bin/env bash
# Install no-Docker Dagster Celery workers on Invoker remotes DO3–DO7.
# Usage: bash scripts/deploy_celery_remotes.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${DO1_SSH_KEY:-$HOME/.ssh/id_ed25519}"
DO1_HOST="${DO1_HOST:-157.245.231.246}"
REMOTE_USER="${DO1_USER:-invoker}"
REMOTE_DIR="/home/invoker/hidden_stock_celery"

REMOTE_HOSTS=(
  "165.22.161.97"   # DO3
  "138.68.230.225"  # DO4
  "68.183.138.39"   # DO5
  "157.230.7.171"   # DO6
  "159.89.237.76"   # DO7
)

RSYNC_EXCLUDES=(
  --exclude .venv --exclude .git --exclude .cache --exclude __pycache__
  --exclude '*.pyc' --exclude .telemetry --exclude hidden_stock.egg-info --exclude .dagster
)

echo "==> Ensure ufw allowlist on DO1 (idempotent; do not force-disable SSH)"
ssh -i "$KEY" -o ConnectTimeout=20 "${REMOTE_USER}@${DO1_HOST}" bash -s <<EOF
set -euo pipefail
sudo ufw allow OpenSSH || true
for ip in ${REMOTE_HOSTS[*]}; do
  sudo ufw allow from "\$ip" to any port 5432 proto tcp || true
  sudo ufw allow from "\$ip" to any port 6379 proto tcp || true
done
# Enable only if already active or user intends lock-down — keep SSH allowed first
if sudo ufw status | grep -qi inactive; then
  echo "ufw inactive; leaving inactive (rules queued). Enable manually after verifying SSH."
else
  sudo ufw status | head -20 || true
fi
EOF

install_one() {
  local host="$1"
  echo "==> Provision celery worker on ${host}"
  rsync -az "${RSYNC_EXCLUDES[@]}" -e "ssh -i $KEY -o ConnectTimeout=25 -o ServerAliveInterval=10" \
    "$ROOT/" "${REMOTE_USER}@${host}:${REMOTE_DIR}/"
  if [[ -f "$ROOT/.env" ]]; then
    scp -i "$KEY" -o ConnectTimeout=20 "$ROOT/.env" "${REMOTE_USER}@${host}:${REMOTE_DIR}/.env"
  fi
  ssh -i "$KEY" -o ConnectTimeout=25 -o ServerAliveInterval=10 "${REMOTE_USER}@${host}" \
    "DO1_HOST=${DO1_HOST} REMOTE_DIR=${REMOTE_DIR}" bash -s <<'EOF'
set -euo pipefail
cd "$REMOTE_DIR"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip >/dev/null
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e .
export DAGSTER_HOME="${REMOTE_DIR}/.dagster_home"
mkdir -p "$DAGSTER_HOME"
cat > "$DAGSTER_HOME/dagster.yaml" <<YAML
storage:
  postgres:
    postgres_db:
      hostname: ${DO1_HOST}
      username: dagster
      password: dagster
      db_name: dagster
      port: 5432
YAML
cat > "${REMOTE_DIR}/celery_worker.yaml" <<YAML
execution:
  config:
    broker: redis://${DO1_HOST}:6379/0
    backend: redis://${DO1_HOST}:6379/0
YAML
pkill -f "dagster-celery worker" 2>/dev/null || true
pkill -f "celery.*dagster_celery" 2>/dev/null || true
sleep 1
export DAGSTER_CELERY_BROKER_URL="redis://${DO1_HOST}:6379/0"
export DAGSTER_CELERY_BACKEND_URL="redis://${DO1_HOST}:6379/0"
nohup env \
  DAGSTER_HOME="$DAGSTER_HOME" \
  POSTGRES_HOST="${DO1_HOST}" \
  POSTGRES_PORT=5432 \
  POSTGRES_USER=dagster \
  POSTGRES_PASSWORD=dagster \
  POSTGRES_DB=dagster \
  DAGSTER_CELERY_BROKER_URL="$DAGSTER_CELERY_BROKER_URL" \
  DAGSTER_CELERY_BACKEND_URL="$DAGSTER_CELERY_BACKEND_URL" \
  dagster-celery worker start -A dagster_celery.app \
  -y "${REMOTE_DIR}/celery_worker.yaml" \
  -n "hs-$(hostname)-$$" \
  > "${REMOTE_DIR}/celery_worker.log" 2>&1 &
sleep 4
if pgrep -af "celery -A dagster_celery" | grep -v pgrep | head -8; then
  echo "worker_ok on $(hostname -I | awk '{print $1}')"
else
  echo "worker_failed"
  tail -80 "${REMOTE_DIR}/celery_worker.log" || true
  exit 1
fi
EOF
}

fail=0
for h in "${REMOTE_HOSTS[@]}"; do
  if ! install_one "$h"; then
    echo "WARN: failed on $h" >&2
    fail=1
  fi
done
if [[ "$fail" -ne 0 ]]; then
  echo "Some remotes failed" >&2
  exit 1
fi
echo "DONE celery remotes"
