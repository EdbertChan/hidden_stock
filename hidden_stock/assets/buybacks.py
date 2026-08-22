"""Share buybacks / net stock issuance as-of a date via EODHD fundamentals.

Cash-flow field ``salePurchaseOfStock`` is net (negative ≈ repurchase).
We sum the last four quarters whose *filing_date* is on or before as_of
(same no-lookahead rule as BVPS). Balance-sheet ``treasuryStock`` is the
stock of cumulative treasury shares (usually negative in EODHD).

Pipeline placement: parallel enrichment off ``historical_deletions``, then
left-joined into ``backtest_summary`` on (ticker, deletion_date) — same
pattern as ``backtest_insider_ownership``. Live screener uses the latest
known TTM for ``stock_summary``.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import dagster as dg
import diskcache
import pandas as pd
import requests
from dagster import AssetExecutionContext

from ..backtest_lib import _eodhd_float, _eodhd_quarterly
from ..resources.db_resource import DBResource

TABLE = "buybacks"
BACKTEST_TABLE = "backtest_buybacks"
COLUMNS = [
    "ticker",
    "deletion_date",
    "as_of_date",
    "period_end",
    "filing_date",
    "sale_purchase_of_stock_ttm",
    "net_buyback_usd_ttm",
    "gross_repurchase_usd_ttm",
    "issuance_of_capital_stock_ttm",
    "treasury_stock",
    "quarters_used",
    "source",
    "note",
]

_FILTER = (
    "Financials::Cash_Flow::quarterly,Financials::Balance_Sheet::quarterly"
)
_fund_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/eodhd_buybacks"))
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _fetch_fundamentals(ticker: str) -> dict | None:
    cached = _fund_cache.get(ticker)
    if cached is not None:
        return cached

    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key:
        raise RuntimeError("EODHD_API_KEY not set")

    resp = requests.get(
        f"https://eodhd.com/api/v1.1/fundamentals/{ticker}.US",
        params={"api_token": api_key, "filter": _FILTER, "fmt": "json"},
        timeout=60,
    )
    if resp.status_code == 404:
        _fund_cache.set(ticker, None, expire=_CACHE_TTL_SECONDS)
        return None
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        data = {}
    _fund_cache.set(ticker, data, expire=_CACHE_TTL_SECONDS)
    return data


def _cf_rows(fundamentals: dict) -> list[dict]:
    quarterly = _eodhd_quarterly(fundamentals, "Cash_Flow")
    rows = []
    for rec in quarterly.values():
        if not isinstance(rec, dict):
            continue
        period = rec.get("date")
        filed = rec.get("filing_date") or period
        if not filed:
            continue
        rows.append(
            {
                "period_end": str(period)[:10] if period else None,
                "filing_date": str(filed)[:10],
                "sale_purchase": _eodhd_float(rec.get("salePurchaseOfStock")),
                "issuance": _eodhd_float(rec.get("issuanceOfCapitalStock")),
            }
        )
    rows.sort(key=lambda r: r["filing_date"])
    return rows


def _bs_rows(fundamentals: dict) -> list[dict]:
    quarterly = _eodhd_quarterly(fundamentals, "Balance_Sheet")
    rows = []
    for rec in quarterly.values():
        if not isinstance(rec, dict):
            continue
        period = rec.get("date")
        filed = rec.get("filing_date") or period
        if not filed:
            continue
        rows.append(
            {
                "period_end": str(period)[:10] if period else None,
                "filing_date": str(filed)[:10],
                "treasury_stock": _eodhd_float(rec.get("treasuryStock")),
            }
        )
    rows.sort(key=lambda r: r["filing_date"])
    return rows


def buybacks_as_of(ticker: str, as_of: str, deletion_date: str | None = None) -> dict:
    """TTM net stock sale/purchase and treasury stock known on or before as_of."""
    as_of = str(as_of)[:10]
    empty = {
        "ticker": ticker,
        "deletion_date": (str(deletion_date)[:10] if deletion_date else None),
        "as_of_date": as_of,
        "period_end": None,
        "filing_date": None,
        "sale_purchase_of_stock_ttm": None,
        "net_buyback_usd_ttm": None,
        "gross_repurchase_usd_ttm": None,
        "issuance_of_capital_stock_ttm": None,
        "treasury_stock": None,
        "quarters_used": 0,
        "source": "eodhd_cash_flow_ttm",
        "note": None,
    }

    try:
        fundamentals = _fetch_fundamentals(ticker)
    except Exception as e:
        empty["note"] = f"EODHD error: {e}"
        return empty

    if not fundamentals:
        empty["note"] = "EODHD fundamentals 404"
        return empty

    known_cf = [r for r in _cf_rows(fundamentals) if r["filing_date"] <= as_of]
    if not known_cf:
        empty["note"] = "no cash-flow quarters filed on/before as_of"
    else:
        # Last 4 filed quarters (may be fewer near IPO / sparse history).
        window = known_cf[-4:]
        sales = [r["sale_purchase"] for r in window if r["sale_purchase"] is not None]
        issues = [r["issuance"] for r in window if r["issuance"] is not None]
        empty["quarters_used"] = len(window)
        empty["period_end"] = window[-1]["period_end"]
        empty["filing_date"] = window[-1]["filing_date"]
        if sales:
            ttm = float(sum(sales))
            empty["sale_purchase_of_stock_ttm"] = ttm
            # Positive = dollars spent on net repurchase after issuances.
            empty["net_buyback_usd_ttm"] = float(max(0.0, -ttm))
            # Gross: sum of negative quarters only (captures buybacks offset by issuance).
            empty["gross_repurchase_usd_ttm"] = float(sum(-v for v in sales if v < 0))
        if issues:
            empty["issuance_of_capital_stock_ttm"] = float(sum(issues))
        empty["note"] = (
            f"TTM salePurchaseOfStock over {len(window)} quarters "
            f"(filed ≤ {as_of}); negative sale_purchase = net buyback; "
            "gross_repurchase = sum of repurchase-only quarters"
        )

    known_bs = [r for r in _bs_rows(fundamentals) if r["filing_date"] <= as_of]
    if known_bs:
        latest_bs = known_bs[-1]
        empty["treasury_stock"] = latest_bs["treasury_stock"]
        if empty["period_end"] is None:
            empty["period_end"] = latest_bs["period_end"]
            empty["filing_date"] = latest_bs["filing_date"]
        if empty["note"] is None:
            empty["note"] = f"treasuryStock from BS filed {latest_bs['filing_date']}"
        else:
            empty["note"] += f"; treasuryStock filed {latest_bs['filing_date']}"
    elif empty["note"] is None:
        empty["note"] = "no balance-sheet quarters filed on/before as_of"

    return empty


def fetch_buybacks_current(ticker: str) -> dict:
    """Live screener: TTM buybacks as of today."""
    today = pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    row = buybacks_as_of(ticker, today, deletion_date=None)
    row["source"] = "eodhd_cash_flow_ttm_current"
    return row


@dg.asset(group_name="buybacks")
def buybacks(
    context: AssetExecutionContext,
    screening_candidates: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    """Live path: latest EODHD TTM buybacks for PB-screened candidates."""
    existing = db.read_table_if_exists(TABLE)
    already = set(existing["ticker"]) if not existing.empty else set()
    tickers = (
        screening_candidates["ticker"].dropna().astype(str).unique().tolist()
        if not screening_candidates.empty
        else []
    )
    todo = [t for t in tickers if t not in already]
    new_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_buybacks_current, t): t for t in todo}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                new_rows.append(fut.result())
            except Exception as e:
                context.log.error(f"EODHD buybacks failed for {ticker}: {e}")

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    if not new_df.empty:
        new_df.to_sql(TABLE, db.get_engine(), schema="stock_data", if_exists="append", index=False)

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    for col in COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[COLUMNS] if not combined.empty else pd.DataFrame(columns=COLUMNS)
    context.add_output_metadata({"num_rows": len(combined), "num_new": len(new_df)})
    return combined


@dg.asset(group_name="backtest")
def backtest_buybacks(
    context: AssetExecutionContext,
    historical_deletions: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    """Backtest: EODHD TTM buybacks / treasury stock as-of each deletion date."""
    existing = db.read_table_if_exists(BACKTEST_TABLE)
    already: set[tuple[str, str]] = set()
    if not existing.empty and "deletion_date" in existing.columns:
        already = set(
            zip(existing["ticker"].astype(str), existing["deletion_date"].astype(str).str[:10])
        )

    if historical_deletions.empty:
        empty = pd.DataFrame(columns=COLUMNS)
        empty.to_sql(BACKTEST_TABLE, db.get_engine(), schema="stock_data", if_exists="replace", index=False)
        return empty

    pairs = (
        historical_deletions[["ticker", "reconstitution_date"]]
        .dropna()
        .assign(
            ticker=lambda d: d["ticker"].astype(str),
            reconstitution_date=lambda d: d["reconstitution_date"].astype(str).str[:10],
        )
        .drop_duplicates()
    )
    todo = [
        (r.ticker, r.reconstitution_date)
        for r in pairs.itertuples()
        if (r.ticker, r.reconstitution_date) not in already
    ]
    context.log.info(f"buybacks todo={len(todo)} cached={len(already)}")

    new_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(buybacks_as_of, t, d, d): (t, d) for t, d in todo
        }
        for fut in as_completed(futures):
            t, d = futures[fut]
            try:
                new_rows.append(fut.result())
            except Exception as e:
                context.log.error(f"buybacks failed for {t} @{d}: {e}")
                new_rows.append(
                    {
                        "ticker": t,
                        "deletion_date": d,
                        "as_of_date": d,
                        "period_end": None,
                        "filing_date": None,
                        "sale_purchase_of_stock_ttm": None,
                        "net_buyback_usd_ttm": None,
                        "gross_repurchase_usd_ttm": None,
                        "issuance_of_capital_stock_ttm": None,
                        "treasury_stock": None,
                        "quarters_used": 0,
                        "source": "eodhd_cash_flow_ttm",
                        "note": f"error: {e}",
                    }
                )

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    if not new_df.empty:
        new_df.to_sql(
            BACKTEST_TABLE, db.get_engine(), schema="stock_data", if_exists="append", index=False
        )

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    for col in COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[COLUMNS] if not combined.empty else pd.DataFrame(columns=COLUMNS)
    context.add_output_metadata(
        {
            "num_rows": len(combined),
            "num_new": len(new_df),
            "num_with_buyback": int(combined["net_buyback_usd_ttm"].notna().sum())
            if len(combined)
            else 0,
        }
    )
    return combined
