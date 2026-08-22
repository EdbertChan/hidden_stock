#!/usr/bin/env python3
"""Equity holdings + QoQ 13F history report.

  python scripts/report_equity_holdings.py BABA
  python scripts/report_equity_holdings.py --live BABA
  python scripts/report_equity_holdings.py --history BABA
"""

from __future__ import annotations

import argparse
import os

import pandas as pd
from sqlalchemy import create_engine


def _engine():
    return create_engine(
        f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )


def _print_current(hold: pd.DataFrame, roll: pd.DataFrame) -> None:
    print("=== parent rollups ===")
    if roll.empty:
        print("(none)")
    else:
        cols = [
            c
            for c in [
                "ticker",
                "holdings_count",
                "holdings_book_adj_usd",
                "holdings_lookthrough_mtm_usd",
                "extract_error",
            ]
            if c in roll.columns
        ]
        print(roll[cols].to_string(index=False))

    print("\n=== current holdings ===")
    if hold.empty:
        print("(none)")
    else:
        cols = [
            c
            for c in [
                "parent_ticker",
                "investee_name",
                "investee_ticker",
                "ownership_pct",
                "shares_held",
                "gaap_treatment",
                "carrying_usd",
                "market_value_usd",
                "note",
            ]
            if c in hold.columns
        ]
        print(hold[cols].to_string(index=False))


def _print_history(hist: pd.DataFrame) -> None:
    print("\n=== QoQ position history (13F) ===")
    if hist.empty:
        print("(none)")
        return
    cols = [
        c
        for c in [
            "period_end",
            "investee_ticker",
            "investee_name",
            "action",
            "shares_held",
            "shares_prev",
            "shares_delta",
            "market_value_usd",
            "filing_date",
            "first_seen_period",
            "exited_period",
        ]
        if c in hist.columns
    ]
    focus = hist[hist["action"].isin(["exit", "sell", "buy", "new"])].copy()
    print("--- exits / sells / buys / new ---")
    if focus.empty:
        print("(none)")
    else:
        print(focus[cols].sort_values(["period_end", "action"]).to_string(index=False))
    print("\n--- full QoQ grid (tail) ---")
    print(hist[cols].sort_values(["period_end", "investee_ticker"]).tail(50).to_string(index=False))


def _live(tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    from hidden_stock.quirks.holdings import process_parent_holdings, rollup_holdings
    from hidden_stock.quirks.holdings.schema import HOLDINGS_COLUMNS
    from hidden_stock.resources.edgar_resource import EdgarResource

    edgar = EdgarResource(user_agent=os.environ["SEC_EDGAR_USER_AGENT"])
    all_rows: list[dict] = []
    rolls: list[dict] = []
    for t in tickers:
        parent = "BRK-B" if t.upper() in {"BRK.B", "BRKB", "BRK-B"} else t.upper()
        rows, meta = process_parent_holdings(
            parent_ticker=parent, edgar=edgar, llm=None, use_llm_fallback=False
        )
        all_rows.extend(rows)
        roll = rollup_holdings(rows)
        rolls.append(
            {
                **roll,
                "ticker": parent,
                "as_of_date": meta.get("filing_date"),
                "accession_no": meta.get("accession_no"),
                "extract_error": meta.get("error"),
            }
        )
        print(
            f"live {parent}: rows={len(rows)} err={meta.get('error')} filing={meta.get('filing_date')}"
        )

    hold = (
        pd.DataFrame(all_rows, columns=HOLDINGS_COLUMNS)
        if all_rows
        else pd.DataFrame(columns=HOLDINGS_COLUMNS)
    )
    roll = pd.DataFrame(rolls)
    eng = _engine()
    hold.to_sql("equity_holdings", eng, schema="stock_data", if_exists="replace", index=False)
    roll.to_sql(
        "equity_holdings_parent_rollups", eng, schema="stock_data", if_exists="replace", index=False
    )
    return hold, roll


def _history(tickers: list[str]) -> pd.DataFrame:
    from hidden_stock.quirks.holdings import HISTORY_COLUMNS, build_13f_history
    from hidden_stock.resources.edgar_resource import EdgarResource

    edgar = EdgarResource(user_agent=os.environ["SEC_EDGAR_USER_AGENT"])
    all_rows: list[dict] = []
    for t in tickers:
        parent = "BRK-B" if t.upper() in {"BRK.B", "BRKB", "BRK-B"} else t.upper()
        rows, meta = build_13f_history(parent_ticker=parent, edgar=edgar, max_filings=40)
        print(
            f"history {parent}: periods={meta.get('num_periods')} rows={len(rows)} err={meta.get('error')}"
        )
        all_rows.extend(rows)
    hist = (
        pd.DataFrame(all_rows, columns=HISTORY_COLUMNS)
        if all_rows
        else pd.DataFrame(columns=HISTORY_COLUMNS)
    )
    hist.to_sql(
        "equity_holdings_history", _engine(), schema="stock_data", if_exists="replace", index=False
    )
    return hist


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="*", default=["BABA"])
    p.add_argument("--live", action="store_true")
    p.add_argument("--history", action="store_true", help="Build/print QoQ 13F history")
    args = p.parse_args()
    tickers = [t.upper() for t in args.tickers]

    if args.live:
        hold, roll = _live(tickers)
    else:
        eng = _engine()
        try:
            hold = pd.read_sql(
                "select * from stock_data.equity_holdings where parent_ticker = any(%(t)s)",
                eng,
                params={"t": tickers},
            )
        except Exception:
            hold = pd.DataFrame()
        try:
            roll = pd.read_sql(
                "select * from stock_data.equity_holdings_parent_rollups where ticker = any(%(t)s)",
                eng,
                params={"t": tickers},
            )
        except Exception:
            roll = pd.DataFrame()

    _print_current(hold, roll)

    if args.history:
        hist = _history(tickers)
        _print_history(hist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
