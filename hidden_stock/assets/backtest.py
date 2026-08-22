import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import dagster as dg
import diskcache
import pandas as pd
import requests
import yfinance as yf

from ..backtest_lib import (
    build_bvps_series_from_eodhd,
    build_earnings_series_from_eodhd,
    earnings_positive_at,
    find_pb_crossing,
)
from ..config import BacktestConfig
from ..resources.db_resource import DBResource
from ..resources.edgar_resource import EdgarResource
from ..resources.llm_protocol import FilingLLM

# Protocol types need ResourceParam or Dagster treats them as asset inputs.
from ..resources.reconstitution_resource import ReconstitutionResource
from .business_description import COLUMNS as BUSINESS_DESCRIPTION_COLUMNS
from .business_description import describe_ticker
from .lifo_fifo import COLUMNS as LIFO_COLUMNS
from .lifo_fifo import classify_ticker

CROSSING_TABLE = "pb_crossing_events"
CROSSING_COLUMNS = [
    "ticker",
    "deletion_date",
    "source_index",
    "status",
    "crossing_date",
    "buy_price",
    "bvps_at_crossing",
    "shares_at_crossing",
    "pb_ratio_at_crossing",
    "bvps_source",
    "profitable_at_crossing",
    "net_income_at_crossing",
    "earnings_source",
    "note",
]
REENTRY_TABLE = "reentry_events"
BACKTEST_LIFO_TABLE = "backtest_lifo_fifo_classifications"
BACKTEST_BUSINESS_DESCRIPTION_TABLE = "backtest_business_descriptions"
RETURNS_TABLE = "backtest_returns"


@dg.asset(group_name="backtest")
def historical_deletions(
    context: dg.AssetExecutionContext, config: BacktestConfig, reconstitution: ReconstitutionResource
) -> pd.DataFrame:
    df = reconstitution.get_deletions(log=context.log)
    if config.ticker_limit:
        all_tickers = list(dict.fromkeys(df["ticker"].tolist()))  # unique, preserve order
        force = [t for t in config.force_tickers if t in set(all_tickers)]
        rest = [t for t in all_tickers if t not in set(force)]
        keep = force + rest[: max(0, config.ticker_limit - len(force))]
        df = df[df["ticker"].isin(keep)].reset_index(drop=True)
    context.add_output_metadata(
        {
            "num_deletions": len(df),
            "num_periods": df["reconstitution_date"].nunique() if len(df) else 0,
            "num_unique_tickers": df["ticker"].nunique() if len(df) else 0,
            "force_tickers": config.force_tickers,
        }
    )
    return df


@dg.asset(group_name="backtest")
def historical_additions(
    context: dg.AssetExecutionContext, reconstitution: ReconstitutionResource
) -> pd.DataFrame:
    df = reconstitution.get_additions(log=context.log)
    context.add_output_metadata(
        {"num_additions": len(df), "num_periods": df["reconstitution_date"].nunique() if len(df) else 0}
    )
    return df


_price_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/yfinance_prices"))
_fundamentals_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/eodhd_fundamentals"))
_PRICE_CACHE_TTL_SECONDS = 24 * 60 * 60
_log = logging.getLogger(__name__)
_EODHD_FUNDAMENTALS_FILTER = (
    "Financials::Balance_Sheet::quarterly,Financials::Income_Statement::quarterly"
)


def _fetch_eodhd_fundamentals(ticker: str) -> dict:
    """One v1.1 fundamentals call — quarterly book and earnings. Cached."""
    cached = _fundamentals_cache.get(ticker)
    if cached is not None:
        return cached
    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key:
        raise RuntimeError("EODHD_API_KEY not set")
    resp = requests.get(
        f"https://eodhd.com/api/v1.1/fundamentals/{ticker}.US",
        params={"api_token": api_key, "filter": _EODHD_FUNDAMENTALS_FILTER},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"EODHD fundamentals for {ticker} returned {type(data).__name__}")
    _fundamentals_cache.set(ticker, data, expire=_PRICE_CACHE_TTL_SECONDS)
    return data


def _fetch_price_history_eodhd(
    ticker: str, start_date: str, end_date: str | None = None, period: str = "d"
) -> pd.DataFrame:
    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key:
        raise RuntimeError("EODHD_API_KEY not set")
    params = {"api_token": api_key, "fmt": "json", "from": start_date, "period": period}
    if end_date:
        params["to"] = end_date
    resp = requests.get(f"https://eodhd.com/api/eod/{ticker}.US", params=params, timeout=20)
    if resp.status_code == 404:
        return pd.DataFrame(columns=["date", "close"])
    resp.raise_for_status()
    data = resp.json()
    if not data or "warning" in data[0]:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.DataFrame(data)[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"])

    # A free/limited EODHD plan silently truncates history (e.g. to the last
    # ~1 year) instead of erroring — no "warning" field, just a later first
    # date than requested. Trusting that as-is would misclassify real P/B
    # crossings before the truncation point as "never crossed". If the
    # earliest returned date is well after the requested start, treat this
    # as a failed fetch so the caller falls back to yfinance's full history.
    # Monthly bars can start up to ~31 days after `from`, so allow a wider gap.
    slack_days = 45 if period == "m" else 30
    requested_start = pd.Timestamp(start_date)
    if not df.empty and df["date"].min() > requested_start + pd.Timedelta(days=slack_days):
        raise RuntimeError(
            f"EODHD returned truncated history for {ticker} "
            f"(earliest {df['date'].min().date()}, requested from {start_date}) — likely a plan limit"
        )
    return df


def _fetch_price_history(
    ticker: str, start_date: str, end_date: str | None = None, period: str = "d"
) -> pd.DataFrame:
    """Disk-cached by (ticker, start_date, end_date, period). Crossing only
    needs monthly bars (`period='m'`); daily is reserved for return windows.

    EODHD (paid, if EODHD_API_KEY is set) is tried first — it has explicit
    delisted-ticker support, which matters here since most of these tickers
    were dropped from an index. Falls back to yfinance (with retry/backoff,
    since it rate-limits aggressively under concurrent load — "Too Many
    Requests" left unhandled looks identical to "no price data" and silently
    corrupts results as insufficient_data) if EODHD is unset or fails."""
    cache_key = (ticker, start_date, end_date, period)
    cached = _price_cache.get(cache_key)
    if cached is not None:
        return cached

    if os.environ.get("EODHD_API_KEY"):
        try:
            result = _fetch_price_history_eodhd(ticker, start_date, end_date, period=period)
            _price_cache.set(cache_key, result, expire=_PRICE_CACHE_TTL_SECONDS)
            return result
        except Exception as e:
            _log.warning(f"EODHD price fetch failed for {ticker}, falling back to yfinance: {e}")

    last_error = None
    for attempt in range(4):
        try:
            hist = yf.Ticker(ticker).history(start=start_date, end=end_date, auto_adjust=False)
            if hist.empty:
                result = pd.DataFrame(columns=["date", "close"])
            else:
                hist = hist.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "close"})
                hist["date"] = pd.to_datetime(hist["date"]).dt.tz_localize(None)
                result = hist
            _price_cache.set(cache_key, result, expire=_PRICE_CACHE_TTL_SECONDS)
            return result
        except Exception as e:
            last_error = e
            if "Too Many Requests" not in str(e) and "Rate limited" not in str(e):
                raise
            time.sleep(2**attempt * 3)  # 3s, 6s, 12s, 24s
    raise last_error


def _process_crossing(ticker: str, deletion_date: str, source_index: str, log) -> dict:
    """Generally-right P/B check: one EODHD fundamentals call (quarterly book
    + earnings) and monthly EOD bars. No daily prices, no EDGAR tag stitching."""
    row = {"ticker": ticker, "deletion_date": deletion_date, "source_index": source_index}
    try:
        prices = _fetch_price_history(ticker, deletion_date, period="m")
    except Exception as e:
        log.warning(f"monthly price history failed for {ticker}: {e}")
        prices = pd.DataFrame(columns=["date", "close"])

    if prices.empty:
        row.update(
            {"status": "insufficient_data", "note": "No monthly price history after deletion date."}
        )
        return row

    try:
        fundamentals = _fetch_eodhd_fundamentals(ticker)
    except Exception as e:
        log.warning(f"EODHD fundamentals failed for {ticker}: {e}")
        row.update({"status": "insufficient_data", "note": f"EODHD fundamentals unavailable: {e}"})
        return row

    bvps_series, citation = build_bvps_series_from_eodhd(fundamentals)
    if bvps_series.empty:
        row.update(
            {
                "status": "insufficient_data",
                "note": "EODHD fundamentals exist but lack quarterly equity/shares.",
            }
        )
        return row

    crossing = find_pb_crossing(prices, bvps_series, deletion_date)
    if not crossing:
        row.update(
            {
                "status": "never_crossed",
                "note": f"BVPS available ({citation}); no monthly close with 0<P/B<1 after deletion.",
            }
        )
        return row

    earnings_series, earnings_citation = build_earnings_series_from_eodhd(fundamentals)
    profitable = earnings_positive_at(earnings_series, crossing["crossing_date"])
    net_income_at_crossing = None
    if profitable is not None:
        known = earnings_series[earnings_series["filed"] <= pd.Timestamp(crossing["crossing_date"])]
        net_income_at_crossing = float(known.sort_values("filed").iloc[-1]["net_income"])

    row.update(crossing)
    row.update(
        {
            "bvps_source": citation,
            "profitable_at_crossing": profitable,
            "net_income_at_crossing": net_income_at_crossing,
            "earnings_source": earnings_citation,
        }
    )
    if profitable is True:
        row["status"] = "crossed"
    elif profitable is False:
        row["status"] = "crossed_unprofitable"
        row["note"] = "0<P/B<1 at crossing, but most recent net income was negative (P/E <= 0)."
    else:
        row["status"] = "crossed_earnings_unknown"
        row["note"] = "0<P/B<1 at crossing, but no earnings data available as of that date."
    return row


@dg.asset(group_name="backtest")
def pb_crossing_events(
    context: dg.AssetExecutionContext,
    historical_deletions: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    existing = db.read_table_if_exists(CROSSING_TABLE)
    already_done = (
        set(zip(existing["ticker"], existing["deletion_date"])) if not existing.empty else set()
    )
    triples = [
        (r.ticker, r.reconstitution_date, r.source_index)
        for r in historical_deletions.itertuples()
        if (r.ticker, r.reconstitution_date) not in already_done
    ]

    new_rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_process_crossing, t, d, si, context.log): (t, d) for t, d, si in triples
        }
        for fut in as_completed(futures):
            t, d = futures[fut]
            try:
                new_rows.append(fut.result())
            except Exception as e:
                context.log.error(f"pb_crossing_events failed for {t} ({d}): {e}")

    new_df = pd.DataFrame(new_rows, columns=CROSSING_COLUMNS)
    if not new_df.empty:
        db.ensure_columns(
            CROSSING_TABLE,
            {"shares_at_crossing": "DOUBLE PRECISION"},
        )
        engine = db.get_engine()
        new_df.to_sql(CROSSING_TABLE, engine, schema="stock_data", if_exists="append", index=False)

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    context.add_output_metadata(
        {
            "num_new": len(new_df),
            "num_total": len(combined),
            "num_crossed_profitable": int((combined["status"] == "crossed").sum()) if len(combined) else 0,
            "num_crossed_unprofitable": int((combined["status"] == "crossed_unprofitable").sum())
            if len(combined)
            else 0,
            "num_crossed_earnings_unknown": int(
                (combined["status"] == "crossed_earnings_unknown").sum()
            )
            if len(combined)
            else 0,
            "num_never_crossed": int((combined["status"] == "never_crossed").sum()) if len(combined) else 0,
            "num_insufficient_data": int((combined["status"] == "insufficient_data").sum())
            if len(combined)
            else 0,
        }
    )
    return combined


@dg.asset(group_name="backtest")
def reentry_events(
    context: dg.AssetExecutionContext,
    pb_crossing_events: pd.DataFrame,
    historical_additions: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    crossed = pb_crossing_events[pb_crossing_events["status"] == "crossed"].copy()
    rows = []
    if not crossed.empty:
        crossed["crossing_date"] = pd.to_datetime(crossed["crossing_date"])
        additions = historical_additions.copy()
        additions["reconstitution_date"] = pd.to_datetime(additions["reconstitution_date"])
        for r in crossed.itertuples():
            matches = additions[
                (additions["ticker"] == r.ticker) & (additions["reconstitution_date"] > r.crossing_date)
            ]
            reentry_date = (
                matches["reconstitution_date"].min().date().isoformat() if not matches.empty else None
            )
            rows.append({"ticker": r.ticker, "deletion_date": r.deletion_date, "reentry_date": reentry_date})

    df = pd.DataFrame(rows, columns=["ticker", "deletion_date", "reentry_date"])
    engine = db.get_engine()
    df.to_sql(REENTRY_TABLE, engine, schema="stock_data", if_exists="replace", index=False)
    context.add_output_metadata(
        {"num_reentered": int(df["reentry_date"].notna().sum()) if len(df) else 0, "num_total": len(df)}
    )
    return df


@dg.asset(group_name="backtest")
def backtest_lifo_fifo(
    context: dg.AssetExecutionContext,
    historical_deletions: pd.DataFrame,
    pb_crossing_events: pd.DataFrame,
    db: DBResource,
    edgar: EdgarResource,
    llm: dg.ResourceParam[FilingLLM],
) -> pd.DataFrame:
    existing = db.read_table_if_exists(BACKTEST_LIFO_TABLE)
    already_done = (
        set(zip(existing["ticker"], existing["accession_no"])) if not existing.empty else set()
    )
    # Reclassify LIFO rows missing the adjustment bundle dollars.
    if not existing.empty:
        for _, row in existing.iterrows():
            missing_usd = "lifo_reserve_usd" not in existing.columns or pd.isna(
                row.get("lifo_reserve_usd")
            )
            if row.get("method") == "LIFO" and row.get("lifo_reserve_disclosed") is True and missing_usd:
                already_done = {pair for pair in already_done if pair[0] != row["ticker"]}

    # Prefer the earliest deletion date per ticker for as_of filing lookup.
    deletions = historical_deletions.sort_values("reconstitution_date").drop_duplicates(
        "ticker", keep="first"
    )
    crossing_by_ticker = {}
    if not pb_crossing_events.empty:
        crossed = pb_crossing_events[
            pb_crossing_events["status"].isin(
                ["crossed", "crossed_unprofitable", "crossed_earnings_unknown"]
            )
        ]
        for r in crossed.itertuples():
            crossing_by_ticker[r.ticker] = {
                "price": getattr(r, "buy_price", None),
                "book_value": getattr(r, "bvps_at_crossing", None),
                "shares": getattr(r, "shares_at_crossing", None),
            }

    new_rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for r in deletions.itertuples():
            valuation = crossing_by_ticker.get(r.ticker)
            futures[
                pool.submit(
                    classify_ticker,
                    context,
                    r.ticker,
                    edgar,
                    llm,
                    already_done,
                    valuation,
                    r.reconstitution_date,
                )
            ] = r.ticker
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result = fut.result()
                if result:
                    new_rows.append(result)
            except Exception as e:
                context.log.error(f"backtest LIFO/FIFO classification failed for {ticker}: {e}")

    new_df = pd.DataFrame(new_rows, columns=LIFO_COLUMNS)
    if not new_df.empty:
        db.ensure_columns(
            BACKTEST_LIFO_TABLE,
            {
                "accounting_basis": "TEXT",
                "lifo_reserve_usd": "DOUBLE PRECISION",
                "adjusted_bvps": "DOUBLE PRECISION",
                "adjusted_pb_ratio": "DOUBLE PRECISION",
            },
        )
        engine = db.get_engine()
        new_df.to_sql(BACKTEST_LIFO_TABLE, engine, schema="stock_data", if_exists="append", index=False)

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    for col in LIFO_COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    if not combined.empty:
        combined = combined[[c for c in LIFO_COLUMNS if c in combined.columns]]
    context.add_output_metadata(
        {
            "num_new": len(new_df),
            "num_total": len(combined),
            "num_with_lifo_adjustment": int(combined["adjusted_bvps"].notna().sum())
            if len(combined) and "adjusted_bvps" in combined.columns
            else 0,
        }
    )
    return combined


@dg.asset(group_name="backtest")
def backtest_business_descriptions(
    context: dg.AssetExecutionContext,
    historical_deletions: pd.DataFrame,
    db: DBResource,
    edgar: EdgarResource,
    llm: dg.ResourceParam[FilingLLM],
) -> pd.DataFrame:
    existing = db.read_table_if_exists(BACKTEST_BUSINESS_DESCRIPTION_TABLE)
    already_done = (
        set(zip(existing["ticker"], existing["accession_no"])) if not existing.empty else set()
    )
    tickers = historical_deletions["ticker"].unique().tolist()

    new_rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(describe_ticker, context, t, edgar, llm, already_done): t for t in tickers
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result = fut.result()
                if result:
                    new_rows.append(result)
            except Exception as e:
                context.log.error(f"backtest business description failed for {ticker}: {e}")

    new_df = pd.DataFrame(new_rows, columns=BUSINESS_DESCRIPTION_COLUMNS)
    if not new_df.empty:
        engine = db.get_engine()
        new_df.to_sql(
            BACKTEST_BUSINESS_DESCRIPTION_TABLE, engine, schema="stock_data", if_exists="append", index=False
        )

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    context.add_output_metadata({"num_new": len(new_df), "num_total": len(combined)})
    return combined


def _compute_return(row: dict, log) -> dict:
    ticker = row["ticker"]
    buy_price = row["buy_price"]
    try:
        if pd.notna(row.get("reentry_date")):
            end_date = (pd.Timestamp(row["reentry_date"]) + pd.Timedelta(days=365)).date().isoformat()
            hist = _fetch_price_history(ticker, row["reentry_date"], end_date)
            if hist.empty:
                return {
                    "ticker": ticker,
                    "source_index": row.get("source_index"),
                    "status": "insufficient_data",
                    "note": "Re-entered the index but no price history in the 12mo window after.",
                }
            best = hist.loc[hist["close"].idxmax()]
            sell_price = float(best["close"])
            return {
                "ticker": ticker,
                "deletion_date": row["deletion_date"],
                "source_index": row.get("source_index"),
                "buy_date": row["crossing_date"],
                "buy_price": buy_price,
                "status": "realized",
                "reentry_date": row["reentry_date"],
                "sell_date": best["date"].date().isoformat(),
                "sell_price": sell_price,
                "return_pct": (sell_price - buy_price) / buy_price * 100,
            }
        else:
            recent_start = (pd.Timestamp.today() - pd.Timedelta(days=10)).date().isoformat()
            hist = _fetch_price_history(ticker, recent_start)
            if hist.empty:
                return {
                    "ticker": ticker,
                    "source_index": row.get("source_index"),
                    "status": "insufficient_data",
                    "note": "No re-entry and no current price available (likely delisted).",
                }
            last = hist.iloc[-1]
            sell_price = float(last["close"])
            return {
                "ticker": ticker,
                "deletion_date": row["deletion_date"],
                "source_index": row.get("source_index"),
                "buy_date": row["crossing_date"],
                "buy_price": buy_price,
                "status": "still_holding",
                "reentry_date": None,
                "sell_date": last["date"].date().isoformat(),
                "sell_price": sell_price,
                "return_pct": (sell_price - buy_price) / buy_price * 100,
            }
    except Exception as e:
        log.error(f"backtest_returns failed for {ticker}: {e}")
        return {"ticker": ticker, "source_index": row.get("source_index"), "status": "error", "note": str(e)}


@dg.asset(group_name="backtest")
def backtest_returns(
    context: dg.AssetExecutionContext,
    pb_crossing_events: pd.DataFrame,
    reentry_events: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    crossed = pb_crossing_events[pb_crossing_events["status"] == "crossed"]
    if not crossed.empty:
        crossed = crossed.merge(reentry_events, on=["ticker", "deletion_date"], how="left")

    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_compute_return, row.to_dict(), context.log): row["ticker"]
            for _, row in crossed.iterrows()
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                rows.append(result)

    df = pd.DataFrame(rows)
    engine = db.get_engine()
    df.to_sql(RETURNS_TABLE, engine, schema="stock_data", if_exists="replace", index=False)
    realized_mask = df["status"].isin(["realized", "still_holding"]) if len(df) else pd.Series(dtype=bool)
    context.add_output_metadata(
        {
            "num_rows": len(df),
            "num_realized": int((df["status"] == "realized").sum()) if len(df) else 0,
            "num_still_holding": int((df["status"] == "still_holding").sum()) if len(df) else 0,
            "avg_return_pct": float(df.loc[realized_mask, "return_pct"].mean()) if realized_mask.any() else None,
        }
    )
    return df


OWNERSHIP_SUMMARY_COLS = [
    "percent_insiders",
    "percent_institutions",
    "percent_non_institutions",
    "insider_shares",
    "institutional_shares",
    "shares_outstanding",
    "insider_owner_count",
    "institutional_holder_count",
    "institutional_period",
    "as_of_date",
]

BUYBACK_SUMMARY_COLS = [
    "sale_purchase_of_stock_ttm",
    "net_buyback_usd_ttm",
    "gross_repurchase_usd_ttm",
    "issuance_of_capital_stock_ttm",
    "treasury_stock",
    "buyback_period_end",
    "buyback_filing_date",
    "buyback_quarters_used",
]


@dg.asset(group_name="backtest")
def backtest_summary(
    context: dg.AssetExecutionContext,
    backtest_returns: pd.DataFrame,
    backtest_lifo_fifo: pd.DataFrame,
    backtest_business_descriptions: pd.DataFrame,
    backtest_insider_ownership: pd.DataFrame,
    backtest_buybacks: pd.DataFrame,
    pb_crossing_events: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    """Final table: every PB-crossing attempt (all statuses), with LIFO
    adjustment bundle, ownership, buybacks, and returns when available.
    SENEA-style crossed_unprofitable rows stay visible with adjusted_pb_ratio."""
    if pb_crossing_events.empty:
        df = pd.DataFrame()
    else:
        df = pb_crossing_events.copy()

    if not backtest_returns.empty and not df.empty:
        # Prefer returns fields that don't collide with crossing columns.
        ret = backtest_returns.rename(
            columns={
                "status": "return_status",
                "buy_price": "return_buy_price",
                "note": "return_note",
            }
        )
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
    if not backtest_lifo_fifo.empty and not df.empty:
        latest = (
            backtest_lifo_fifo.sort_values("filing_date")
            .groupby("ticker", as_index=False)
            .tail(1)
            .rename(columns={"source_quote": "lifo_source_quote"})
        )
        merge_cols = [c for c in ["ticker", *lifo_cols, "lifo_source_quote"] if c in latest.columns]
        df = df.merge(latest[merge_cols], on="ticker", how="left")
        for col in [*lifo_cols, "lifo_source_quote"]:
            if col not in df.columns:
                df[col] = None
    elif not df.empty:
        for col in [*lifo_cols, "lifo_source_quote"]:
            df[col] = None

    if not backtest_business_descriptions.empty and not df.empty:
        latest_desc = (
            backtest_business_descriptions.sort_values("filing_date")
            .groupby("ticker", as_index=False)
            .tail(1)
            .rename(columns={"source_quote": "description_source_quote"})
        )
        df = df.merge(
            latest_desc[["ticker", "description", "description_source_quote"]], on="ticker", how="left"
        )
    elif not df.empty:
        df["description"] = None
        df["description_source_quote"] = None

    if not backtest_insider_ownership.empty and not df.empty:
        own = backtest_insider_ownership.copy()
        own["deletion_date"] = own["deletion_date"].astype(str).str[:10]
        df["deletion_date"] = df["deletion_date"].astype(str).str[:10]
        # Prefer exact (ticker, deletion_date); fall back to latest ticker row.
        merge_cols = [c for c in ["ticker", "deletion_date", *OWNERSHIP_SUMMARY_COLS] if c in own.columns]
        df = df.merge(own[merge_cols], on=["ticker", "deletion_date"], how="left")
        for col in OWNERSHIP_SUMMARY_COLS:
            if col not in df.columns:
                df[col] = None
    elif not df.empty:
        for col in OWNERSHIP_SUMMARY_COLS:
            df[col] = None

    if not backtest_buybacks.empty and not df.empty:
        bb = backtest_buybacks.copy()
        bb["deletion_date"] = bb["deletion_date"].astype(str).str[:10]
        df["deletion_date"] = df["deletion_date"].astype(str).str[:10]
        bb = bb.rename(
            columns={
                "period_end": "buyback_period_end",
                "filing_date": "buyback_filing_date",
                "quarters_used": "buyback_quarters_used",
            }
        )
        merge_cols = [c for c in ["ticker", "deletion_date", *BUYBACK_SUMMARY_COLS] if c in bb.columns]
        df = df.merge(bb[merge_cols], on=["ticker", "deletion_date"], how="left")
        for col in BUYBACK_SUMMARY_COLS:
            if col not in df.columns:
                df[col] = None
    elif not df.empty:
        for col in BUYBACK_SUMMARY_COLS:
            df[col] = None

    holdings_cols = [
        "holdings_count",
        "holdings_book_adj_usd",
        "bvps_holdings_adj",
        "pb_holdings_adj",
        "holdings_lookthrough_mtm_usd",
        "holdings_above_count",
        "holdings_below_count",
        "holdings_ni_impact_count",
    ]
    engine = db.get_engine()
    try:
        hold_roll = pd.read_sql(
            "select * from stock_data.backtest_equity_holdings_parent_rollups", engine
        )
    except Exception:
        hold_roll = pd.DataFrame()
    if not hold_roll.empty and not df.empty and "ticker" in hold_roll.columns:
        merge_cols = [c for c in ["ticker", *holdings_cols] if c in hold_roll.columns]
        df = df.merge(hold_roll[merge_cols], on="ticker", how="left")
        for col in holdings_cols:
            if col not in df.columns:
                df[col] = None
    elif not df.empty:
        for col in holdings_cols:
            df[col] = None

    df.to_sql("backtest_summary", engine, schema="stock_data", if_exists="replace", index=False)
    context.add_output_metadata(
        {
            "num_rows": len(df),
            "num_lifo_adjusted": int(df["adjusted_bvps"].notna().sum())
            if len(df) and "adjusted_bvps" in df.columns
            else 0,
            "num_with_adjusted_pb": int(df["adjusted_pb_ratio"].notna().sum())
            if len(df) and "adjusted_pb_ratio" in df.columns
            else 0,
            "num_with_insiders": int(df["percent_insiders"].notna().sum())
            if len(df) and "percent_insiders" in df.columns
            else 0,
            "num_with_non_inst": int(df["percent_non_institutions"].notna().sum())
            if len(df) and "percent_non_institutions" in df.columns
            else 0,
            "num_with_buybacks": int(df["net_buyback_usd_ttm"].notna().sum())
            if len(df) and "net_buyback_usd_ttm" in df.columns
            else 0,
            "num_with_holdings": int(
                pd.to_numeric(df.get("holdings_count"), errors="coerce").fillna(0).gt(0).sum()
            )
            if len(df) and "holdings_count" in df.columns
            else 0,
        }
    )
    return df
