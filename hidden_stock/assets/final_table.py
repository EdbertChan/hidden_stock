import dagster as dg
import pandas as pd

from ..resources.db_resource import DBResource

TABLE = "stock_summary"
# Classification fields + LIFO adjustment bundle (all-or-nothing when applied).
LIFO_COLUMNS = [
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


OWNERSHIP_COLUMNS = [
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

BUYBACK_COLUMNS = [
    "sale_purchase_of_stock_ttm",
    "net_buyback_usd_ttm",
    "gross_repurchase_usd_ttm",
    "issuance_of_capital_stock_ttm",
    "treasury_stock",
    "buyback_period_end",
    "buyback_filing_date",
    "buyback_quarters_used",
]

HOLDINGS_PARENT_COLUMNS = [
    "holdings_count",
    "holdings_book_adj_usd",
    "bvps_holdings_adj",
    "pb_holdings_adj",
    "holdings_lookthrough_mtm_usd",
    "holdings_above_count",
    "holdings_below_count",
    "holdings_ni_impact_count",
]


@dg.asset(group_name="final")
def stock_summary(
    context: dg.AssetExecutionContext,
    index_dropouts: pd.DataFrame,
    price_book_screen: pd.DataFrame,
    lifo_fifo_classifications: pd.DataFrame,
    business_descriptions: pd.DataFrame,
    insider_ownership: pd.DataFrame,
    buybacks: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    df = price_book_screen.copy()
    dropout_tickers = set(index_dropouts["ticker"]) if not index_dropouts.empty else set()
    df["is_index_dropout"] = df["ticker"].isin(dropout_tickers)

    if not lifo_fifo_classifications.empty:
        latest = (
            lifo_fifo_classifications.sort_values("filing_date")
            .groupby("ticker", as_index=False)
            .tail(1)
            .rename(columns={"source_quote": "lifo_source_quote"})
        )
        merge_cols = [c for c in ["ticker", *LIFO_COLUMNS, "lifo_source_quote"] if c in latest.columns]
        df = df.merge(latest[merge_cols], on="ticker", how="left")
        for col in [*LIFO_COLUMNS, "lifo_source_quote"]:
            if col not in df.columns:
                df[col] = None
    else:
        for col in [*LIFO_COLUMNS, "lifo_source_quote"]:
            df[col] = None

    if not business_descriptions.empty:
        latest_desc = (
            business_descriptions.sort_values("filing_date")
            .groupby("ticker", as_index=False)
            .tail(1)
            .rename(columns={"source_quote": "description_source_quote"})
        )
        df = df.merge(
            latest_desc[["ticker", "description", "description_source_quote"]], on="ticker", how="left"
        )
    else:
        df["description"] = None
        df["description_source_quote"] = None

    if not insider_ownership.empty:
        own = insider_ownership.drop_duplicates("ticker", keep="last")
        merge_cols = [c for c in ["ticker", *OWNERSHIP_COLUMNS] if c in own.columns]
        df = df.merge(own[merge_cols], on="ticker", how="left")
        for col in OWNERSHIP_COLUMNS:
            if col not in df.columns:
                df[col] = None
    else:
        for col in OWNERSHIP_COLUMNS:
            df[col] = None

    if not buybacks.empty:
        bb = buybacks.drop_duplicates("ticker", keep="last").rename(
            columns={
                "period_end": "buyback_period_end",
                "filing_date": "buyback_filing_date",
                "quarters_used": "buyback_quarters_used",
            }
        )
        merge_cols = [c for c in ["ticker", *BUYBACK_COLUMNS] if c in bb.columns]
        df = df.merge(bb[merge_cols], on="ticker", how="left")
        for col in BUYBACK_COLUMNS:
            if col not in df.columns:
                df[col] = None
    else:
        for col in BUYBACK_COLUMNS:
            df[col] = None

    # Parent holdings rollups (from equity_holdings asset side table, or compute).
    engine = db.get_engine()
    try:
        roll = pd.read_sql("select * from stock_data.equity_holdings_parent_rollups", engine)
    except Exception:
        roll = pd.DataFrame()
    if not roll.empty and "ticker" in roll.columns:
        merge_cols = [c for c in ["ticker", *HOLDINGS_PARENT_COLUMNS] if c in roll.columns]
        df = df.merge(roll[merge_cols], on="ticker", how="left")
        for col in HOLDINGS_PARENT_COLUMNS:
            if col not in df.columns:
                df[col] = None
    else:
        for col in HOLDINGS_PARENT_COLUMNS:
            df[col] = None

    df.to_sql(TABLE, engine, schema="stock_data", if_exists="replace", index=False)
    context.add_output_metadata(
        {
            "num_rows": len(df),
            "num_dropouts": int(df["is_index_dropout"].sum()) if len(df) else 0,
            "num_classified": int(df["method"].notna().sum()) if len(df) else 0,
            "num_lifo_adjusted": int(df["adjusted_bvps"].notna().sum()) if len(df) else 0,
            "num_with_insiders": int(df["percent_insiders"].notna().sum()) if len(df) else 0,
            "num_with_non_inst": int(df["percent_non_institutions"].notna().sum()) if len(df) else 0,
            "num_with_buybacks": int(df["net_buyback_usd_ttm"].notna().sum()) if len(df) else 0,
            "num_with_holdings": int(pd.to_numeric(df.get("holdings_count"), errors="coerce").fillna(0).gt(0).sum())
            if len(df) and "holdings_count" in df.columns
            else 0,
        }
    )
    return df
