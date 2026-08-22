#!/usr/bin/env bash
# Monitor 13F fanout workers; restart dead remotes/local; optional merge.
# Target concurrency: 6 DO boxes × 2 threads = 12 (no Claude — sec-api only).
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
WORKERS_PER_BOX="${WORKERS_PER_BOX:-2}"
INTERVAL="${MONITOR_INTERVAL:-60}"
LOG="$WORK/logs/monitor.log"
mkdir -p "$WORK/logs" "$WORK/results"

MACHINES=(
  "do1:invoker@157.245.231.246"
  "do3:invoker@165.22.161.97"
  "do4:invoker@138.68.230.225"
  "do5:invoker@68.183.138.39"
  "do6:invoker@157.230.7.171"
  "do7:invoker@159.89.237.76"
)

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }

restart_remote() {
  local name="$1" target="$2"
  log "RESTART $name on $target"
  ssh -o ConnectTimeout=15 -i "$SSH_KEY" "$target" "bash -lc '
    set -a; source $REMOTE_DIR/.env; set +a
    cd $REMOTE_DIR
    export PYTHONPATH=$REMOTE_DIR
    # kill old if zombie pid file
    if [ -f results/${name}.pid ]; then kill \$(cat results/${name}.pid) 2>/dev/null || true; fi
    nohup python3 scripts/ownership_13f_worker.py \
      --chunk chunks/assigned.csv \
      --out results/${name}.jsonl \
      --workers $WORKERS_PER_BOX \
      >> results/${name}.run.log 2>&1 < /dev/null &
    echo \$! > results/${name}.pid
    echo restarted pid=\$(cat results/${name}.pid)
  '" >>"$LOG" 2>&1 || log "FAIL restart $name"
}

restart_local() {
  log "RESTART local"
  if [ -f "$WORK/results/local.pid" ]; then
    kill "$(cat "$WORK/results/local.pid")" 2>/dev/null || true
  fi
  (
    export PYTHONPATH="$ROOT"
    nohup "$ROOT/.venv/bin/python" "$ROOT/scripts/ownership_13f_worker.py" \
      --chunk "$WORK/chunks/chunk_06.csv" \
      --out "$WORK/results/local.jsonl" \
      --workers "$WORKERS_PER_BOX" \
      >> "$WORK/logs/local.run.log" 2>&1 < /dev/null &
    echo $! > "$WORK/results/local.pid"
  )
  log "local pid=$(cat "$WORK/results/local.pid")"
}

check_once() {
  local running=0 dead=0 total_lines=0
  for entry in "${MACHINES[@]}"; do
    local name="${entry%%:*}" target="${entry#*:}"
    local st lines
    st=$(ssh -o ConnectTimeout=10 -i "$SSH_KEY" "$target" \
      "pid=\$(cat $REMOTE_DIR/results/${name}.pid 2>/dev/null);
       if kill -0 \$pid 2>/dev/null; then echo RUNNING; else echo DEAD; fi" 2>/dev/null || echo UNREACHABLE)
    lines=$(ssh -o ConnectTimeout=10 -i "$SSH_KEY" "$target" \
      "wc -l < $REMOTE_DIR/results/${name}.jsonl 2>/dev/null || echo 0" 2>/dev/null || echo 0)
    lines=$(echo "$lines" | tr -d '[:space:]')
    total_lines=$((total_lines + ${lines:-0}))
    if [ "$st" = "RUNNING" ]; then
      running=$((running + 1))
      log "$name RUNNING lines=$lines (×$WORKERS_PER_BOX threads)"
    else
      dead=$((dead + 1))
      log "$name $st lines=$lines — restarting"
      # only restart if chunk not finished
      done_line=$(ssh -o ConnectTimeout=10 -i "$SSH_KEY" "$target" \
        "tail -n 5 $REMOTE_DIR/results/${name}.run.log 2>/dev/null | grep -c '^done wrote=' || true" 2>/dev/null || echo 0)
      if [ "${done_line:-0}" -ge 1 ]; then
        log "$name finished cleanly — no restart"
      else
        restart_remote "$name" "$target"
      fi
    fi
  done

  # local optional (extra beyond the 12 remote threads)
  local_pid_file="$WORK/results/local.pid"
  if [ -f "$WORK/chunks/chunk_06.csv" ]; then
    if [ -f "$local_pid_file" ] && kill -0 "$(cat "$local_pid_file")" 2>/dev/null; then
      ll=$(wc -l < "$WORK/results/local.jsonl" 2>/dev/null || echo 0)
      total_lines=$((total_lines + ll))
      log "local RUNNING lines=$ll"
      running=$((running + 1))
    else
      if grep -q '^done wrote=' "$WORK/logs/local.run.log" 2>/dev/null; then
        log "local finished cleanly"
      else
        dead=$((dead + 1))
        restart_local
      fi
    fi
  fi

  local concurrent=$((running * WORKERS_PER_BOX))
  log "summary boxes_up~$running dead=$dead ~threads=$concurrent jsonl_lines=$total_lines"
  # merge snapshot periodically
  if [ $((total_lines % 50)) -lt 20 ] || [ "$dead" -eq 0 ]; then
    bash "$ROOT/scripts/ownership_13f_collect.sh" >>"$LOG" 2>&1 || log "collect failed"
  fi
}

log "monitor start interval=${INTERVAL}s target=${#MACHINES[@]}×${WORKERS_PER_BOX}=$(( ${#MACHINES[@]} * WORKERS_PER_BOX )) threads (no Claude)"
while true; do
  check_once || log "check_once error"
  sleep "$INTERVAL"
done
