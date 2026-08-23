#!/usr/bin/env python3
"""Validate holdings pipeline stages with mechanical + Fable/Codex swarm.

  uv run python scripts/swarm_validate_pipeline.py \\
    --parent TCEHY --stages broker,overlay,composition --judges fable,codex

Aliases: broker→broker_pdf_parse, overlay→overlay_merge,
composition→composition_export.

Writes exports/<ticker>_swarm_<stage>_{mechanical,fable,codex,board}.json
and exports/<ticker>_swarm_<stage>_board.md.

Mechanical FAIL hard on the board. LLM judges still run for recommended_fixes.
On BOARD FAIL / NEEDS_WORK follow thrash-reflect-automate (same as sheet swarm).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

STAGE_ALIASES = {
    "broker": "broker_pdf_parse",
    "broker_pdf": "broker_pdf_parse",
    "broker_pdf_parse": "broker_pdf_parse",
    "overlay": "overlay_merge",
    "overlay_merge": "overlay_merge",
    "composition": "composition_export",
    "composition_export": "composition_export",
    "catalog": "catalog_parse",
    "catalog_parse": "catalog_parse",
    "hk": "hk_annual_parse",
    "hk_annual": "hk_annual_parse",
    "hk_annual_parse": "hk_annual_parse",
    "sec": "sec_13g_parse",
    "sec_13g": "sec_13g_parse",
    "sec_13g_parse": "sec_13g_parse",
    "sheet": "sheet_export",
    "sheet_export": "sheet_export",
}


def main() -> int:
    from hidden_stock.quirks.holdings.parents import normalize_parent
    from hidden_stock.quirks.holdings.swarm_verify import (
        ALL_STAGES,
        PIPELINE_SCHEMA_PATH,
        DEFAULT_SCHEMA_PATH,
        load_dotenv,
        load_schema,
        validate_stage,
        write_stage_board_md,
    )

    load_dotenv(_ROOT)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent", default="TCEHY")
    p.add_argument(
        "--stages",
        default="broker,overlay,composition",
        help="Comma list of stages (aliases ok)",
    )
    p.add_argument(
        "--judges",
        default="fable,codex",
        help="Comma list: fable,codex (mechanical always runs)",
    )
    p.add_argument("--exports-dir", default=str(_ROOT / "exports"))
    p.add_argument("--out-dir", default=None, help="Default: same as exports-dir")
    args = p.parse_args()

    parent = normalize_parent(args.parent)
    exports_dir = Path(args.exports_dir)
    out_dir = Path(args.out_dir or args.exports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stages_raw = [s.strip() for s in args.stages.split(",") if s.strip()]
    stages: list[str] = []
    for s in stages_raw:
        key = s.lower()
        if key not in STAGE_ALIASES:
            print(f"Unknown stage {s!r}; known={sorted(STAGE_ALIASES)}", file=sys.stderr)
            return 2
        stages.append(STAGE_ALIASES[key])

    judges = [j.strip().lower() for j in args.judges.split(",") if j.strip()]
    schema_path = (
        PIPELINE_SCHEMA_PATH if PIPELINE_SCHEMA_PATH.is_file() else DEFAULT_SCHEMA_PATH
    )
    schema = load_schema(schema_path)

    slug = parent.lower()
    overall_ok = True
    boards = []

    for stage in stages:
        print(f"\n=== validating {parent} / {stage} ===", file=sys.stderr)
        board = validate_stage(
            stage,
            parent=parent,
            exports_dir=exports_dir,
            judges=judges,
            schema=schema,
            schema_path=schema_path,
        )
        boards.append(board)
        if not board.get("ok"):
            overall_ok = False

        prefix = out_dir / f"{slug}_swarm_{stage}"
        (prefix.with_name(prefix.name + "_mechanical.json")).write_text(
            json.dumps(board["mechanical"], indent=2) + "\n", encoding="utf-8"
        )
        for j in board.get("judges") or []:
            name = str(j.get("judge") or "judge")
            (out_dir / f"{slug}_swarm_{stage}_{name}.json").write_text(
                json.dumps(j, indent=2) + "\n", encoding="utf-8"
            )
        (out_dir / f"{slug}_swarm_{stage}_board.json").write_text(
            json.dumps(
                {
                    k: board[k]
                    for k in (
                        "stage",
                        "board",
                        "ok",
                        "recommended_fixes",
                        "root_cause_class",
                        "avoid_next_time",
                    )
                    if k in board
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        md = write_stage_board_md(
            parent=parent,
            stage=stage,
            board=board,
            out_path=out_dir / f"{slug}_swarm_{stage}_board.md",
        )
        print(md.read_text(encoding="utf-8"))
        print(f"Wrote {md}", file=sys.stderr)

    summary = {
        "parent": parent,
        "ok": overall_ok,
        "stages": [
            {
                "stage": b["stage"],
                "board": b["board"],
                "root_cause_class": b.get("root_cause_class"),
                "recommended_fixes": b.get("recommended_fixes"),
            }
            for b in boards
        ],
    }
    summary_path = out_dir / f"{slug}_swarm_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nSummary: ok={overall_ok} → {summary_path}", file=sys.stderr)
    # Exit 1 on FAIL so CI/gates can hard-fail mechanical+board (not Celery yet).
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
