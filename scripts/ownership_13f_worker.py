#!/usr/bin/env python3
"""Chunk worker for 13F ownership backfill.

Reads a CSV of ticker,deletion_date pairs; writes one JSON object per line
to --out. Safe for many machines in parallel (no shared DB writes).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--chunk", required=True, help="CSV with ticker,deletion_date")
    p.add_argument("--out", required=True, help="JSONL output path")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--progress-every", type=int, default=10)
    args = p.parse_args()

    # Allow running from a shipped tree: PYTHONPATH=<repo_root>
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = Path(__file__).resolve().parent
    for p in (str(repo_root), str(scripts_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from ownership_core import (  # type: ignore  # noqa: E402
            COLUMNS,
            ownership_as_of_deletion,
            _ownership_error_row,
        )
    except ImportError as e:
        raise SystemExit(
            f"ownership_core import failed: {e}. "
            "Ship scripts/ownership_core.py with the worker."
        ) from e

    pairs: list[tuple[str, str]] = []
    with open(args.chunk, newline="") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip()
            d = (row.get("deletion_date") or "").strip()[:10]
            if t and d:
                pairs.append((t, d))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Resume: skip keys already in out
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        with open(out_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    done.add((str(obj["ticker"]), str(obj["deletion_date"])[:10]))
                except Exception:
                    pass

    todo = [pd for pd in pairs if pd not in done]
    print(
        f"chunk={args.chunk} pairs={len(pairs)} done={len(done)} todo={len(todo)} workers={args.workers}",
        flush=True,
    )
    if not todo:
        print("nothing to do", flush=True)
        return 0

    t0 = time.time()
    finished = 0
    with open(out_path, "a") as out, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(ownership_as_of_deletion, t, d): (t, d) for t, d in todo}
        for fut in as_completed(futs):
            t, d = futs[fut]
            finished += 1
            try:
                row = fut.result()
            except Exception as e:
                row = _ownership_error_row(t, d, e)
            # Ensure all columns present
            for c in COLUMNS:
                row.setdefault(c, None)
            out.write(json.dumps(row, default=str) + "\n")
            out.flush()
            if finished % args.progress_every == 0 or finished == len(todo):
                print(
                    f"progress {finished}/{len(todo)} elapsed={time.time()-t0:.0f}s",
                    flush=True,
                )

    print(f"done wrote={finished} -> {out_path} elapsed={time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
