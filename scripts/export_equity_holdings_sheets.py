#!/usr/bin/env python3
"""Refresh equity holdings and export CSV + Google Sheets (any parent ticker).

  python scripts/export_equity_holdings_sheets.py --ticker TCEHY --live --history --new-sheet
  python scripts/export_equity_holdings_sheets.py --ticker BABA --reuse-sheet \\
    --spreadsheet-id "$GOOGLE_SHEETS_SPREADSHEET_ID"

Env:
  GOOGLE_SHEETS_OAUTH_CLIENT_SECRETS / GOOGLE_SHEETS_OAUTH_TOKEN  (preferred for new sheets)
  GOOGLE_SHEETS_CREDENTIALS_JSON       service-account JSON (write to shared sheet / Shared Drive)
  GOOGLE_SHEETS_DRIVE_FOLDER_ID        Shared Drive folder for SA creates
  GOOGLE_SHEETS_SPREADSHEET_ID         reuse target when --reuse-sheet
  GOOGLE_SHEETS_CREATE_NEW             default 1
  SEC_EDGAR_USER_AGENT, POSTGRES_*
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--ticker",
        required=True,
        help="Parent ticker or company alias (e.g. BABA, TCEHY, tencent, alibaba)",
    )
    p.add_argument("--live", action="store_true", help="Refresh stock_data.equity_holdings")
    p.add_argument("--history", action="store_true", help="Refresh QoQ equity_holdings_history")
    p.add_argument(
        "--lookback-years",
        type=int,
        default=None,
        help=(
            "Primary history depth: calendar years from today/as_of. "
            "Default 8 for fanout_13g_hk (TCEHY), else 5"
        ),
    )
    p.add_argument(
        "--max-filings",
        type=int,
        default=80,
        help="Safety ceiling inside the year window (newest-first, default 80)",
    )
    p.add_argument("--out-dir", default=str(_ROOT / "exports"))
    p.add_argument("--no-sheets", action="store_true", help="Skip Google Sheets push")
    p.add_argument("--spreadsheet-id", default=None, help="Target sheet when reusing")
    sheet = p.add_mutually_exclusive_group()
    sheet.add_argument(
        "--new-sheet",
        action="store_true",
        default=None,
        help="Create a new timestamped spreadsheet (default)",
    )
    sheet.add_argument(
        "--reuse-sheet",
        action="store_true",
        help="Overwrite GOOGLE_SHEETS_SPREADSHEET_ID / --spreadsheet-id",
    )
    args = p.parse_args()

    from hidden_stock.quirks.holdings.export import export_parent
    from hidden_stock.quirks.holdings.parents import history_strategy, normalize_parent

    parent = normalize_parent(args.ticker)
    strategy = history_strategy(parent)
    if args.lookback_years is None:
        lookback_years = 8 if strategy == "fanout_13g_hk" else 5
    else:
        lookback_years = int(args.lookback_years)

    if args.reuse_sheet:
        create_new = False
    elif args.new_sheet:
        create_new = True
    else:
        create_new = None

    result = export_parent(
        parent,
        refresh=bool(args.live),
        history=bool(args.history),
        max_filings=int(args.max_filings),
        lookback_years=lookback_years,
        out_dir=args.out_dir,
        push_sheets=not args.no_sheets,
        spreadsheet_id=args.spreadsheet_id,
        create_new=create_new,
    )
    result["strategy"] = strategy
    print(json.dumps(result, indent=2))
    if result.get("sheets", {}).get("url"):
        print(f"\nSheet: {result['sheets']['url']}", file=sys.stderr)
        print(f"History strategy: {strategy}", file=sys.stderr)
    if result.get("sheets_error"):
        print(f"\nSheets push failed: {result['sheets_error']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
