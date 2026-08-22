import pandas as pd

# Tried in order — companies use different XBRL tags for the same concept.
EQUITY_TAGS = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]
SHARES_TAGS = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
]
EARNINGS_TAGS = [
    "NetIncomeLoss",
    "ProfitLoss",
]


def _extract_fact_series(facts: dict, taxonomy: str, tag: str, unit: str) -> pd.DataFrame:
    try:
        entries = facts["facts"][taxonomy][tag]["units"][unit]
    except (KeyError, TypeError):
        return pd.DataFrame(columns=["end", "filed", "val"])
    df = pd.DataFrame(entries)
    if df.empty or not {"end", "filed", "val"}.issubset(df.columns):
        return pd.DataFrame(columns=["end", "filed", "val"])
    df = df[["end", "filed", "val"]].dropna()
    # pandas 3.0's merge_asof requires exact dtype match between merge keys;
    # explicitly normalize to datetime64[ns] since sources vary (SEC's JSON
    # dates vs yfinance's index) in their inferred unit ([s] vs [us] vs [ns]).
    df["end"] = pd.to_datetime(df["end"]).astype("datetime64[ns]")
    df["filed"] = pd.to_datetime(df["filed"]).astype("datetime64[ns]")
    return df.sort_values("filed")


def build_bvps_series(facts: dict) -> tuple[pd.DataFrame, str | None]:
    """Book value per share over time, keyed by filing date (not period-end
    date) so later lookups never use a number the market couldn't have known
    yet. Returns (DataFrame[filed, bvps], citation) or (empty df, None) if
    the filer doesn't use any of the XBRL tags we check."""
    equity_df, equity_tag_used = pd.DataFrame(), None
    for tag in EQUITY_TAGS:
        equity_df = _extract_fact_series(facts, "us-gaap", tag, "USD")
        if not equity_df.empty:
            equity_tag_used = tag
            break
    if equity_df.empty:
        return pd.DataFrame(columns=["filed", "bvps"]), None

    shares_df, shares_tag_used = pd.DataFrame(), None
    for taxonomy, tag in SHARES_TAGS:
        shares_df = _extract_fact_series(facts, taxonomy, tag, "shares")
        if not shares_df.empty:
            shares_tag_used = f"{taxonomy}:{tag}"
            break
    if shares_df.empty:
        return pd.DataFrame(columns=["filed", "bvps"]), None

    equity_df = equity_df.drop_duplicates("end", keep="last").sort_values("end")
    shares_df = shares_df.drop_duplicates("end", keep="last").sort_values("end")
    merged = pd.merge_asof(
        equity_df,
        shares_df[["end", "val"]].rename(columns={"val": "shares"}),
        on="end",
        direction="nearest",
        tolerance=pd.Timedelta(days=100),
    )
    merged = merged.dropna(subset=["shares"])
    merged = merged[merged["shares"] > 0]
    if merged.empty:
        return pd.DataFrame(columns=["filed", "bvps"]), None

    merged["bvps"] = merged["val"] / merged["shares"]
    merged = merged.sort_values("filed")[["filed", "bvps"]].drop_duplicates("filed", keep="last")
    citation = f"SEC EDGAR XBRL companyfacts: us-gaap:{equity_tag_used} / {shares_tag_used}"
    return merged.reset_index(drop=True), citation


def build_earnings_series(facts: dict) -> tuple[pd.DataFrame, str | None]:
    """Net income over time, keyed by filing date. Used to check P/E > 0
    (i.e. the company was actually profitable), not to compute a P/E ratio
    itself — the ratio's magnitude doesn't matter here, only its sign.
    Returns (DataFrame[filed, net_income], citation) or (empty df, None)."""
    for tag in EARNINGS_TAGS:
        df = _extract_fact_series(facts, "us-gaap", tag, "USD")
        if not df.empty:
            df = df.drop_duplicates("end", keep="last").sort_values("filed")
            df = df.rename(columns={"val": "net_income"})[["filed", "net_income"]]
            df = df.drop_duplicates("filed", keep="last")
            return df.reset_index(drop=True), f"SEC EDGAR XBRL companyfacts: us-gaap:{tag}"
    return pd.DataFrame(columns=["filed", "net_income"]), None


def earnings_positive_at(earnings_series: pd.DataFrame, as_of_date: str) -> bool | None:
    """Was the most recently-filed net income figure, as of as_of_date,
    positive? Returns None (not False) when there's no filing on or before
    that date — an unknown is not the same as a loss."""
    if earnings_series.empty:
        return None
    known = earnings_series[earnings_series["filed"] <= pd.Timestamp(as_of_date)]
    if known.empty:
        return None
    latest = known.sort_values("filed").iloc[-1]
    return bool(latest["net_income"] > 0)


def _eodhd_float(val) -> float | None:
    if val in (None, "None", ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _eodhd_quarterly(payload: dict, statement: str) -> dict:
    """Unwrap a fundamentals filter response to a date-keyed quarterly dict.
    `statement` is Balance_Sheet or Income_Statement.

    EODHD v1.1 filter shapes we have seen:
    - combined filter: {"Financials::Balance_Sheet::quarterly": {date: rec, ...}}
    - single filter: {date: rec, ...}
    - unfiltered / section: {"Financials": {"Balance_Sheet": {"quarterly": {...}}}}
    """
    if not isinstance(payload, dict):
        return {}
    filter_key = f"Financials::{statement}::quarterly"
    candidates = []
    if filter_key in payload:
        candidates.append(payload[filter_key])
    node = payload.get("Financials", payload)
    if statement in node:
        node = node[statement]
    if isinstance(node, dict) and "quarterly" in node:
        candidates.append(node["quarterly"])
    candidates.append(payload)

    for node in candidates:
        if not isinstance(node, dict):
            continue
        sample = next((v for v in node.values() if isinstance(v, dict)), None)
        if sample and any(
            k in sample for k in ("date", "filing_date", "totalStockholderEquity", "netIncome")
        ):
            return node
    return {}


def build_bvps_series_from_eodhd(fundamentals: dict) -> tuple[pd.DataFrame, str | None]:
    """Quarterly book value per share from EODHD fundamentals, keyed by filing
    date. Coarse on purpose — one fundamentals call, not daily EDGAR tags."""
    quarterly = _eodhd_quarterly(fundamentals, "Balance_Sheet")
    rows = []
    for rec in quarterly.values():
        if not isinstance(rec, dict):
            continue
        filed = rec.get("filing_date") or rec.get("date")
        equity = _eodhd_float(rec.get("totalStockholderEquity"))
        shares = _eodhd_float(rec.get("commonStockSharesOutstanding"))
        if not filed or equity is None or not shares or shares <= 0:
            continue
        rows.append({"filed": filed, "bvps": equity / shares, "shares": shares})
    if not rows:
        return pd.DataFrame(columns=["filed", "bvps", "shares"]), None
    df = pd.DataFrame(rows)
    df["filed"] = pd.to_datetime(df["filed"]).astype("datetime64[ns]")
    df = df.sort_values("filed").drop_duplicates("filed", keep="last")
    citation = (
        "EODHD fundamentals v1.1 Financials.Balance_Sheet.quarterly: "
        "totalStockholderEquity / commonStockSharesOutstanding"
    )
    return df.reset_index(drop=True), citation


def build_earnings_series_from_eodhd(fundamentals: dict) -> tuple[pd.DataFrame, str | None]:
    """Quarterly net income from EODHD fundamentals, keyed by filing date."""
    quarterly = _eodhd_quarterly(fundamentals, "Income_Statement")
    rows = []
    for rec in quarterly.values():
        if not isinstance(rec, dict):
            continue
        filed = rec.get("filing_date") or rec.get("date")
        net_income = _eodhd_float(rec.get("netIncome"))
        if not filed or net_income is None:
            continue
        rows.append({"filed": filed, "net_income": net_income})
    if not rows:
        return pd.DataFrame(columns=["filed", "net_income"]), None
    df = pd.DataFrame(rows)
    df["filed"] = pd.to_datetime(df["filed"]).astype("datetime64[ns]")
    df = df.sort_values("filed").drop_duplicates("filed", keep="last")
    return df.reset_index(drop=True), "EODHD fundamentals v1.1 Financials.Income_Statement.quarterly: netIncome"


def find_pb_crossing(price_history: pd.DataFrame, bvps_series: pd.DataFrame, after_date: str) -> dict | None:
    """First price bar on or after `after_date` where 0 < close/bvps < 1.
    Bars can be daily or monthly — we only need to be generally right.
    price_history: DataFrame[date, close]. bvps_series: DataFrame[filed, bvps]."""
    if price_history.empty or bvps_series.empty:
        return None
    prices = price_history.copy()
    prices["date"] = pd.to_datetime(prices["date"]).astype("datetime64[ns]")
    prices = prices[prices["date"] >= pd.Timestamp(after_date)].sort_values("date")
    if prices.empty:
        return None
    merged = pd.merge_asof(
        prices, bvps_series.sort_values("filed"), left_on="date", right_on="filed", direction="backward"
    )
    merged = merged.dropna(subset=["bvps"])
    merged = merged[merged["bvps"] > 0]
    if merged.empty:
        return None
    merged["pb_ratio"] = merged["close"] / merged["bvps"]
    hits = merged[(merged["pb_ratio"] > 0) & (merged["pb_ratio"] < 1)]
    if hits.empty:
        return None
    first = hits.iloc[0]
    out = {
        "crossing_date": first["date"].date().isoformat(),
        "buy_price": float(first["close"]),
        "bvps_at_crossing": float(first["bvps"]),
        "pb_ratio_at_crossing": float(first["pb_ratio"]),
    }
    if "shares" in first.index and pd.notna(first["shares"]):
        out["shares_at_crossing"] = float(first["shares"])
    return out
