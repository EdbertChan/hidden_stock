#!/usr/bin/env bash
# Fan out 13F ownership chunks to DigitalOcean Invoker machines + local.
# Remotes write JSONL only (Postgres is localhost on the Mac).
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

mkdir -p "$WORK/chunks" "$WORK/results" "$WORK/logs"

MACHINES=(
  "do1:invoker@157.245.231.246"
  "do3:invoker@165.22.161.97"
  "do4:invoker@138.68.230.225"
  "do5:invoker@68.183.138.39"
  "do6:invoker@157.230.7.171"
  "do7:invoker@159.89.237.76"
)

echo "== building remaining todo list =="
"$ROOT/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
import pandas as pd
from sqlalchemy import create_engine

work = Path(".cache/ownership_13f_fanout")
engine = create_engine(
    f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
)
pairs = pd.read_sql(
    "select distinct ticker, deletion_date::text as deletion_date from stock_data.pb_crossing_events",
    engine,
)
pairs["deletion_date"] = pairs["deletion_date"].astype(str).str[:10]
try:
    own = pd.read_sql(
        "select ticker, deletion_date::text as deletion_date, percent_non_institutions "
        "from stock_data.backtest_insider_ownership",
        engine,
    )
    own["deletion_date"] = own["deletion_date"].astype(str).str[:10]
    done = set(
        zip(
            own.loc[own["percent_non_institutions"].notna(), "ticker"].astype(str),
            own.loc[own["percent_non_institutions"].notna(), "deletion_date"],
        )
    )
except Exception:
    done = set()
todo = pairs[~pairs.apply(lambda r: (str(r.ticker), str(r.deletion_date)[:10]) in done, axis=1)]
todo = todo[["ticker", "deletion_date"]].drop_duplicates()
todo.to_csv(work / "todo_all.csv", index=False)
print(f"todo={len(todo)} done={len(done)}", flush=True)
PY

N_REMOTE=${#MACHINES[@]}
N_TOTAL=$((N_REMOTE + 1))  # + local
"$ROOT/.venv/bin/python" - <<PY
from pathlib import Path
import pandas as pd
work = Path(".cache/ownership_13f_fanout")
todo = pd.read_csv(work / "todo_all.csv")
n = $N_TOTAL
chunks = [todo.iloc[i::n] for i in range(n)]
for i, c in enumerate(chunks):
    path = work / "chunks" / f"chunk_{i:02d}.csv"
    c.to_csv(path, index=False)
    print(f"chunk_{i:02d} rows={len(c)} -> {path}", flush=True)
PY

# Ship a minimal runnable tree to each remote
BUNDLE="$WORK/bundle"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"
rsync -a --exclude '__pycache__' --exclude '*.pyc' \
  "$ROOT/scripts/ownership_core.py" \
  "$ROOT/scripts/ownership_13f_worker.py" \
  "$BUNDLE/"
mkdir -p "$BUNDLE/scripts"
mv "$BUNDLE/ownership_core.py" "$BUNDLE/ownership_13f_worker.py" "$BUNDLE/scripts/"
# tiny requirements for workers
cat > "$BUNDLE/requirements-worker.txt" <<'REQ'
requests>=2.31
diskcache>=5.6
REQ

# env file for remotes (API keys only — no localhost postgres)
umask 077
cat > "$WORK/remote.env" <<EOF
EODHD_API_KEY=${EODHD_API_KEY}
SEC_API_KEY=${SEC_API_KEY}
EOF

deploy_one() {
  local name="$1" target="$2" chunk_idx="$3"
  local chunk="$WORK/chunks/chunk_$(printf '%02d' "$chunk_idx").csv"
  local log="$WORK/logs/${name}.log"
  echo "== deploy $name ($target) chunk=$chunk_idx ==" | tee -a "$log"
  ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" "$target" \
    "rm -rf $REMOTE_DIR/hidden_stock; mkdir -p $REMOTE_DIR/chunks $REMOTE_DIR/results $REMOTE_DIR/scripts" >>"$log" 2>&1
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$BUNDLE/scripts" "$BUNDLE/requirements-worker.txt" \
    "$target:$REMOTE_DIR/" >>"$log" 2>&1
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$chunk" "$WORK/remote.env" \
    "$target:$REMOTE_DIR/chunks/" >>"$log" 2>&1
  # rename env + chunk to stable names on remote
  local chunk_base
  chunk_base="$(basename "$chunk")"
  ssh -i "$SSH_KEY" "$target" "cp $REMOTE_DIR/chunks/remote.env $REMOTE_DIR/.env; cp $REMOTE_DIR/chunks/$chunk_base $REMOTE_DIR/chunks/assigned.csv" >>"$log" 2>&1
  ssh -i "$SSH_KEY" "$target" "python3 -m pip install --user -q -r $REMOTE_DIR/requirements-worker.txt" >>"$log" 2>&1 || true
}

run_remote() {
  local name="$1" target="$2"
  local log="$WORK/logs/${name}.log"
  echo "== run $name ==" | tee -a "$log"
  ssh -i "$SSH_KEY" "$target" "bash -lc '
    set -a; source $REMOTE_DIR/.env; set +a
    cd $REMOTE_DIR
    export PYTHONPATH=$REMOTE_DIR
    nohup python3 scripts/ownership_13f_worker.py \
      --chunk chunks/assigned.csv \
      --out results/${name}.jsonl \
      --workers $WORKERS_PER_BOX \
      > results/${name}.run.log 2>&1 < /dev/null &
    echo \$! > results/${name}.pid
    echo started pid=\$(cat results/${name}.pid)
  '" | tee -a "$log"
}

# Deploy all remotes in parallel
i=0
for entry in "${MACHINES[@]}"; do
  name="${entry%%:*}"
  target="${entry#*:}"
  deploy_one "$name" "$target" "$i" &
  i=$((i + 1))
done
wait
echo "== all remotes deployed =="

# Start remotes
i=0
for entry in "${MACHINES[@]}"; do
  name="${entry%%:*}"
  target="${entry#*:}"
  run_remote "$name" "$target"
  i=$((i + 1))
done

# Local chunk = last index
LOCAL_IDX=$N_REMOTE
LOCAL_CHUNK="$WORK/chunks/chunk_$(printf '%02d' "$LOCAL_IDX").csv"
echo "== run local chunk $LOCAL_IDX =="
(
  set -a; source "$ROOT/.env"; set +a
  export PYTHONPATH="$ROOT"
  nohup "$ROOT/.venv/bin/python" "$ROOT/scripts/ownership_13f_worker.py" \
    --chunk "$LOCAL_CHUNK" \
    --out "$WORK/results/local.jsonl" \
    --workers "$WORKERS_PER_BOX" \
    > "$WORK/logs/local.run.log" 2>&1 < /dev/null &
  echo $! > "$WORK/results/local.pid"
  echo "local pid=$(cat "$WORK/results/local.pid")"
)

echo "== fanout launched =="
echo "Pull+merge later with: bash scripts/ownership_13f_collect.sh"
