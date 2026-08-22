"""Dagster assets: equity holdings ledger + parent rollups."""

from concurrent.futures import ThreadPoolExecutor, as_completed

import dagster as dg
import pandas as pd
from sqlalchemy import text

from ..config import EquityHoldingsConfig
from ..quirks.holdings import (
    HOLDINGS_COLUMNS,
    HISTORY_COLUMNS,
    PARENT_ROLLUP_COLUMNS,
    build_13f_history,
    process_parent_holdings,
    rollup_holdings,
)
from ..resources.db_resource import DBResource
from ..resources.edgar_resource import EdgarResource
from ..resources.llm_protocol import FilingLLM

TABLE = "equity_holdings"
BACKTEST_TABLE = "backtest_equity_holdings"
PARENT_ROLLUP_TABLE = "equity_holdings_parent_rollups"
HISTORY_TABLE = "equity_holdings_history"


def _ensure_table(engine, table: str) -> None:
    cols_sql = ",\n".join(f'"{c}" text' for c in HOLDINGS_COLUMNS)
    # Widen numeric-ish columns as double precision via cast on write; keep flexible text OK.
    # Prefer typed DDL for analytics columns:
    typed = {
        "ownership_pct": "double precision",
        "shares_held": "double precision",
        "carrying_usd": "double precision",
        "fair_value_disclosed_usd": "double precision",
        "market_price": "double precision",
        "market_value_usd": "double precision",
        "suggested_adj_usd": "double precision",
        "lookthrough_mtm_usd": "double precision",
        "influence_disclosed": "boolean",
        "impacts_parent_ni": "boolean",
        "include_in_book_adj": "boolean",
        "already_at_market": "boolean",
        "holdings_count": "integer",
        "holdings_above_count": "integer",
        "holdings_below_count": "integer",
        "holdings_ni_impact_count": "integer",
    }
    parts = []
    for c in HOLDINGS_COLUMNS:
        parts.append(f'"{c}" {typed.get(c, "text")}')
    ddl = f"""
    CREATE SCHEMA IF NOT EXISTS stock_data;
    CREATE TABLE IF NOT EXISTS stock_data.{table} (
        {", ".join(parts)}
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


def _write_holdings(engine, table: str, rows: list[dict], replace_parents: list[str] | None = None) -> None:
    _ensure_table(engine, table)
    df = pd.DataFrame(rows, columns=HOLDINGS_COLUMNS) if rows else pd.DataFrame(columns=HOLDINGS_COLUMNS)
    with engine.begin() as conn:
        if replace_parents:
            conn.execute(
                text(f"DELETE FROM stock_data.{table} WHERE parent_ticker = ANY(:parents)"),
                {"parents": replace_parents},
            )
        if not df.empty:
            df.to_sql(table, conn, schema="stock_data", if_exists="append", index=False)


def _ensure_allowlist_rows(df: pd.DataFrame, allowlist: list[str]) -> pd.DataFrame:
    """Include allowlisted tickers even when they are outside the P/B screen."""
    if not allowlist:
        return df
    import yfinance as yf

    have = set(df["ticker"].astype(str).str.upper()) if not df.empty else set()
    rows = [] if df.empty else [df]
    for t in allowlist:
        tu = t.upper()
        if tu in have:
            continue
        try:
            info = yf.Ticker(tu).info
            rows.append(
                pd.DataFrame(
                    [
                        {
                            "ticker": tu,
                            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                            "book_value": info.get("bookValue"),
                            "shares": info.get("sharesOutstanding"),
                            "pb_ratio": info.get("priceToBook"),
                        }
                    ]
                )
            )
        except Exception:
            rows.append(
                pd.DataFrame(
                    [{"ticker": tu, "price": None, "book_value": None, "shares": None, "pb_ratio": None}]
                )
            )
    if not rows:
        return df
    out = pd.concat(rows, ignore_index=True)
    allow = {t.upper() for t in allowlist}
    return out[out["ticker"].astype(str).str.upper().isin(allow)].reset_index(drop=True)


def _run_universe(
    context: dg.AssetExecutionContext,
    tickers_df: pd.DataFrame,
    edgar: EdgarResource,
    llm: FilingLLM,
    db: DBResource,
    config: EquityHoldingsConfig,
    *,
    table: str,
    as_of_col: str | None,
    price_col: str | None = "price",
    bvps_col: str | None = "book_value",
    shares_col: str | None = "shares",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = tickers_df.copy() if not tickers_df.empty else pd.DataFrame(
        columns=["ticker", "price", "book_value", "shares", "pb_ratio"]
    )
    if config.ticker_allowlist:
        df = _ensure_allowlist_rows(df, config.ticker_allowlist)

    if df.empty:
        return pd.DataFrame(columns=HOLDINGS_COLUMNS), pd.DataFrame(columns=["ticker", *PARENT_ROLLUP_COLUMNS])

    work = []
    for r in df.itertuples(index=False):
        ticker = str(getattr(r, "ticker"))
        as_of = str(getattr(r, as_of_col))[:10] if as_of_col and hasattr(r, as_of_col) else None
        price = getattr(r, price_col, None) if price_col else None
        bvps = getattr(r, bvps_col, None) if bvps_col else None
        shares = getattr(r, shares_col, None) if shares_col else None
        work.append((ticker, as_of, price, bvps, shares))

    if not work:
        return pd.DataFrame(columns=HOLDINGS_COLUMNS), pd.DataFrame(columns=["ticker", *PARENT_ROLLUP_COLUMNS])

    all_rows: list[dict] = []
    rollups: list[dict] = []

    def _one(item):
        ticker, as_of, price, bvps, shares = item
        rows, meta = process_parent_holdings(
            parent_ticker=ticker,
            edgar=edgar,
            llm=llm,
            as_of=as_of,
            use_llm_fallback=bool(getattr(config, "use_llm_fallback", False)),
        )
        roll = rollup_holdings(rows, price=price, book_value_per_share=bvps, shares=shares)
        roll["ticker"] = ticker
        roll["as_of_date"] = as_of or meta.get("filing_date")
        roll["accession_no"] = meta.get("accession_no")
        roll["extract_error"] = meta.get("error")
        return rows, roll, meta

    with ThreadPoolExecutor(max_workers=max(1, int(config.max_workers))) as pool:
        futs = {pool.submit(_one, w): w[0] for w in work}
        for fut in as_completed(futs):
            ticker = futs[fut]
            try:
                rows, roll, meta = fut.result()
                all_rows.extend(rows)
                rollups.append(roll)
                context.log.info(
                    f"{ticker}: holdings={len(rows)} err={meta.get('error')} filing={meta.get('filing_date')}"
                )
            except Exception as e:
                context.log.error(f"equity holdings failed for {ticker}: {e}")
                rollups.append(
                    {
                        "ticker": ticker,
                        **{c: None for c in PARENT_ROLLUP_COLUMNS},
                        "holdings_count": 0,
                        "extract_error": str(e),
                    }
                )

    parents = sorted({w[0] for w in work})
    _write_holdings(db.get_engine(), table, all_rows, replace_parents=parents)
    roll_df = pd.DataFrame(rollups)
    return (
        pd.DataFrame(all_rows, columns=HOLDINGS_COLUMNS) if all_rows else pd.DataFrame(columns=HOLDINGS_COLUMNS),
        roll_df,
    )


@dg.asset(group_name="equity_holdings")
def equity_holdings(
    context: dg.AssetExecutionContext,
    config: EquityHoldingsConfig,
    edgar: dg.ResourceParam[EdgarResource],
    llm: dg.ResourceParam[FilingLLM],
    db: dg.ResourceParam[DBResource],
) -> pd.DataFrame:
    """Live path: extract holdings for allowlist and/or current P/B screen table.

    Does not hard-depend on ``screening_candidates`` so ``equity_holdings_job``
    can run allowlist smoke (BABA / BRK.B / TCEHY) without materializing the
    full live screen first. When no allowlist is set, loads
    ``stock_data.price_book_screen`` if present.
    """
    engine = db.get_engine()
    if config.ticker_allowlist:
        base = pd.DataFrame(columns=["ticker", "price", "book_value", "shares", "pb_ratio"])
    else:
        try:
            base = pd.read_sql("select * from stock_data.price_book_screen", engine)
        except Exception:
            base = pd.DataFrame(columns=["ticker", "price", "book_value", "shares", "pb_ratio"])
            context.log.warning("price_book_screen missing; set ticker_allowlist on EquityHoldingsConfig")

    holdings_df, roll_df = _run_universe(
        context,
        base,
        edgar,
        llm,
        db,
        config,
        table=TABLE,
        as_of_col=None,
        price_col="price",
        bvps_col="book_value",
        shares_col="shares",
    )
    # Persist parent rollups for summary merge without recompute.
    if not roll_df.empty:
        roll_df.to_sql(PARENT_ROLLUP_TABLE, engine, schema="stock_data", if_exists="replace", index=False)
    context.add_output_metadata(
        {
            "num_holdings_rows": len(holdings_df),
            "num_parents": int(roll_df["ticker"].nunique()) if len(roll_df) else 0,
            "num_with_book_adj": int(
                (pd.to_numeric(roll_df.get("holdings_book_adj_usd"), errors="coerce").fillna(0) != 0).sum()
            )
            if len(roll_df) and "holdings_book_adj_usd" in roll_df.columns
            else 0,
        }
    )
    return holdings_df


@dg.asset(group_name="equity_holdings")
def equity_holdings_history(
    context: dg.AssetExecutionContext,
    config: EquityHoldingsConfig,
    edgar: dg.ResourceParam[EdgarResource],
    db: dg.ResourceParam[DBResource],
) -> pd.DataFrame:
    """Quarter-over-quarter 13F positions: ticker, shares, buys/sells/exits, period_end."""
    tickers = [t.upper().replace(".", "-") for t in (config.ticker_allowlist or ["BABA"])]
    all_rows: list[dict] = []
    for t in tickers:
        if t in {"TCEHY", "TCTZF"}:
            context.log.warning(f"{t}: no US 13F portfolio history; skip QoQ")
            continue
        rows, meta = build_13f_history(
            parent_ticker=t,
            edgar=edgar,
            max_filings=int(config.history_max_filings),
        )
        context.log.info(
            f"{t}: periods={meta.get('num_periods')} filings={meta.get('num_filings')} "
            f"rows={len(rows)} err={meta.get('error')}"
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows, columns=HISTORY_COLUMNS) if all_rows else pd.DataFrame(columns=HISTORY_COLUMNS)
    engine = db.get_engine()
    df.to_sql(HISTORY_TABLE, engine, schema="stock_data", if_exists="replace", index=False)

    exits = int((df["action"] == "exit").sum()) if len(df) else 0
    sells = int((df["action"] == "sell").sum()) if len(df) else 0
    buys = int((df["action"].isin(["buy", "new"])).sum()) if len(df) else 0
    context.add_output_metadata(
        {
            "num_rows": len(df),
            "num_parents": int(df["parent_ticker"].nunique()) if len(df) else 0,
            "num_exits": exits,
            "num_sells": sells,
            "num_buys_or_new": buys,
            "periods": int(df["period_end"].nunique()) if len(df) else 0,
        }
    )
    return df


@dg.asset(group_name="backtest")
def backtest_equity_holdings(
    context: dg.AssetExecutionContext,
    pb_crossing_events: pd.DataFrame,
    config: EquityHoldingsConfig,
    edgar: dg.ResourceParam[EdgarResource],
    llm: dg.ResourceParam[FilingLLM],
    db: dg.ResourceParam[DBResource],
) -> pd.DataFrame:
    """Backtest path: holdings as-of deletion / crossing inputs."""
    src = pb_crossing_events.copy()
    if not src.empty:
        # Prefer deletion_date as as-of; valuation from crossing.
        if "buy_price" in src.columns:
            src["price"] = src["buy_price"]
        if "bvps_at_crossing" in src.columns:
            src["book_value"] = src["bvps_at_crossing"]
        if "shares_at_crossing" in src.columns:
            src["shares"] = src["shares_at_crossing"]
        src["as_of"] = src["deletion_date"].astype(str).str[:10]

    holdings_df, roll_df = _run_universe(
        context,
        src,
        edgar,
        llm,
        db,
        config,
        table=BACKTEST_TABLE,
        as_of_col="as_of",
        price_col="price",
        bvps_col="book_value",
        shares_col="shares",
    )
    engine = db.get_engine()
    if not roll_df.empty:
        roll_df.to_sql(
            "backtest_equity_holdings_parent_rollups",
            engine,
            schema="stock_data",
            if_exists="replace",
            index=False,
        )
    context.add_output_metadata(
        {
            "num_holdings_rows": len(holdings_df),
            "num_parents": int(roll_df["ticker"].nunique()) if len(roll_df) else 0,
        }
    )
    return holdings_df
