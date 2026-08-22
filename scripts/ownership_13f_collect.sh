#!/usr/bin/env bash
# Pull remote JSONL results and upsert into local Postgres + refresh CSV.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

WORK="$ROOT/.cache/ownership_13f_fanout"
REMOTE_DIR="~/hidden_stock_13f"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
mkdir -p "$WORK/results"

MACHINES=(
  "do1:invoker@157.245.231.246"
  "do3:invoker@165.22.161.97"
  "do4:invoker@138.68.230.225"
  "do5:invoker@68.183.138.39"
  "do6:invoker@157.230.7.171"
  "do7:invoker@159.89.237.76"
)

for entry in "${MACHINES[@]}"; do
  name="${entry%%:*}"
  target="${entry#*:}"
  echo "== pull $name =="
  scp -o ConnectTimeout=15 -i "$SSH_KEY" \
    "$target:$REMOTE_DIR/results/${name}.jsonl" \
    "$WORK/results/${name}.jsonl" 2>/dev/null || echo "  (no jsonl yet)"
  scp -o ConnectTimeout=15 -i "$SSH_KEY" \
    "$target:$REMOTE_DIR/results/${name}.run.log" \
    "$WORK/logs/${name}.run.log" 2>/dev/null || true
  # status
  ssh -o ConnectTimeout=10 -i "$SSH_KEY" "$target" \
    "if [ -f $REMOTE_DIR/results/${name}.pid ]; then
       pid=\$(cat $REMOTE_DIR/results/${name}.pid)
       if kill -0 \$pid 2>/dev/null; then echo running pid=\$pid; else echo finished; fi
       tail -n 2 $REMOTE_DIR/results/${name}.run.log 2>/dev/null || true
     else echo no-pid; fi" 2>/dev/null || echo "  unreachable"
done

if [ -f "$WORK/results/local.pid" ]; then
  if kill -0 "$(cat "$WORK/results/local.pid")" 2>/dev/null; then
    echo "local: running pid=$(cat "$WORK/results/local.pid")"
  else
    echo "local: finished"
  fi
  tail -n 2 "$WORK/logs/local.run.log" 2>/dev/null || true
fi

shopt -s nullglob
FILES=("$WORK/results"/*.jsonl)
if [ ${#FILES[@]} -eq 0 ]; then
  echo "no jsonl files yet"
  exit 0
fi

echo "== merge ${#FILES[@]} jsonl files =="
"$ROOT/.venv/bin/python" "$ROOT/scripts/ownership_13f_merge.py" "${FILES[@]}" \
  --csv-out "$ROOT/backtest_summary_5y.csv"
