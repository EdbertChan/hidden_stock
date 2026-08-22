#!/usr/bin/env python3
"""Merge JSONL ownership worker outputs into Postgres via UPSERT."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from hidden_stock.assets.insider_ownership import COLUMNS, BACKTEST_TABLE
from hidden_stock.assets.backtest import BUYBACK_SUMMARY_COLS, OWNERSHIP_SUMMARY_COLS


def _engine():
    return create_engine(
        f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )


def load_jsonl(paths: list[Path]) -> pd.DataFrame:
    rows = []
    for path in paths:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    df = df[COLUMNS]
    df["deletion_date"] = df["deletion_date"].astype(str).str[:10]
    # Last write wins per key
    df = df.drop_duplicates(["ticker", "deletion_date"], keep="last")
    return df


def upsert(df: pd.DataFrame, engine) -> None:
    if df.empty:
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS stock_data"))
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS stock_data.{BACKTEST_TABLE} (
                    ticker text,
                    deletion_date text,
                    as_of_date text,
                    percent_insiders double precision,
                    percent_institutions double precision,
                    percent_non_institutions double precision,
                    insider_shares double precision,
                    institutional_shares double precision,
                    shares_outstanding double precision,
                    shares_float double precision,
                    insider_owner_count double precision,
                    institutional_holder_count double precision,
                    filings_considered double precision,
                    institutional_period text,
                    source text,
                    note text
                )
                """
            )
        )
        # Add any missing columns from older schema
        existing = {
            r[0]
            for r in conn.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema='stock_data' AND table_name=:t
                    """
                ),
                {"t": BACKTEST_TABLE},
            )
        }
        for col, sql_type in {
            "percent_non_institutions": "double precision",
            "institutional_shares": "double precision",
            "institutional_holder_count": "double precision",
            "institutional_period": "text",
        }.items():
            if col not in existing:
                conn.execute(
                    text(
                        f"ALTER TABLE stock_data.{BACKTEST_TABLE} ADD COLUMN {col} {sql_type}"
                    )
                )
        conn.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {BACKTEST_TABLE}_ticker_deletion_uidx
                ON stock_data.{BACKTEST_TABLE} (ticker, deletion_date)
                """
            )
        )

    tmp = f"{BACKTEST_TABLE}_staging"
    df.to_sql(tmp, engine, schema="stock_data", if_exists="replace", index=False)
    cols = ", ".join(COLUMNS)
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in COLUMNS if c not in ("ticker", "deletion_date"))
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                INSERT INTO stock_data.{BACKTEST_TABLE} ({cols})
                SELECT {cols} FROM stock_data.{tmp}
                ON CONFLICT (ticker, deletion_date) DO UPDATE SET {updates}
                """
            )
        )
        conn.execute(text(f"DROP TABLE stock_data.{tmp}"))


def rebuild_summary(engine) -> pd.DataFrame:
    cross = pd.read_sql("select * from stock_data.pb_crossing_events", engine)
    df = cross.copy()
    df["deletion_date"] = df["deletion_date"].astype(str).str[:10]

    try:
        ret = pd.read_sql("select * from stock_data.backtest_returns", engine)
    except Exception:
        ret = pd.DataFrame()
    if not ret.empty:
        ret = ret.rename(
            columns={"status": "return_status", "buy_price": "return_buy_price", "note": "return_note"}
        )
        ret["deletion_date"] = ret["deletion_date"].astype(str).str[:10]
        join_cols = ["ticker", "deletion_date"]
        extra = [c for c in ret.columns if c not in join_cols]
        df = df.merge(ret[join_cols + extra], on=join_cols, how="left")

    lifo_cols = [
        "accounting_basis",
        "method",
        "lifo_reserve_disclosed",
        "lifo_reserve_usd",
        "method_change_disclosed",
        "quirk_notes",
        "confidence",
        "adjusted_bvps",
        "adjusted_pb_ratio",
    ]
    try:
        lifo = pd.read_sql("select * from stock_data.backtest_lifo_fifo_classifications", engine)
    except Exception:
        lifo = pd.DataFrame()
    if not lifo.empty:
        latest = (
            lifo.sort_values("filing_date")
            .groupby("ticker", as_index=False)
            .tail(1)
            .rename(columns={"source_quote": "lifo_source_quote"})
        )
        merge_cols = [c for c in ["ticker", *lifo_cols, "lifo_source_quote"] if c in latest.columns]
        df = df.merge(latest[merge_cols], on="ticker", how="left")
    else:
        for c in [*lifo_cols, "lifo_source_quote"]:
            df[c] = None

    try:
        desc = pd.read_sql("select * from stock_data.backtest_business_descriptions", engine)
    except Exception:
        desc = pd.DataFrame()
    if not desc.empty:
        latest_desc = (
            desc.sort_values("filing_date")
            .groupby("ticker", as_index=False)
            .tail(1)
            .rename(columns={"source_quote": "description_source_quote"})
        )
        df = df.merge(
            latest_desc[["ticker", "description", "description_source_quote"]],
            on="ticker",
            how="left",
        )
    else:
        df["description"] = None
        df["description_source_quote"] = None

    own = pd.read_sql(f"select * from stock_data.{BACKTEST_TABLE}", engine)
    if not own.empty:
        own["deletion_date"] = own["deletion_date"].astype(str).str[:10]
        merge_cols = [c for c in ["ticker", "deletion_date", *OWNERSHIP_SUMMARY_COLS] if c in own.columns]
        df = df.merge(own[merge_cols], on=["ticker", "deletion_date"], how="left")

    try:
        bb = pd.read_sql("select * from stock_data.backtest_buybacks", engine)
    except Exception:
        bb = pd.DataFrame()
    if not bb.empty:
        bb = bb.rename(
            columns={
                "period_end": "buyback_period_end",
                "filing_date": "buyback_filing_date",
                "quarters_used": "buyback_quarters_used",
            }
        )
        bb["deletion_date"] = bb["deletion_date"].astype(str).str[:10]
        merge_cols = [c for c in ["ticker", "deletion_date", *BUYBACK_SUMMARY_COLS] if c in bb.columns]
        df = df.merge(bb[merge_cols], on=["ticker", "deletion_date"], how="left")

    df.to_sql("backtest_summary", engine, schema="stock_data", if_exists="replace", index=False)
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="+", help="Worker JSONL paths")
    ap.add_argument("--csv-out", default="backtest_summary_5y.csv")
    ap.add_argument("--skip-summary", action="store_true")
    args = ap.parse_args()

    engine = _engine()
    df = load_jsonl([Path(p) for p in args.jsonl])
    print(f"loaded rows={len(df)} non_inst={df['percent_non_institutions'].notna().sum()}")
    upsert(df, engine)
    n = pd.read_sql(
        f"select count(*) n, count(percent_non_institutions) ni from stock_data.{BACKTEST_TABLE}",
        engine,
    )
    print(f"table after upsert: {n.to_dict('records')[0]}")

    if not args.skip_summary:
        summary = rebuild_summary(engine)
        summary.to_csv(args.csv_out, index=False)
        print(
            f"summary rows={len(summary)} non_inst={summary['percent_non_institutions'].notna().sum()} -> {args.csv_out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
