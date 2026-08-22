from datetime import datetime, timezone

import dagster as dg
import pandas as pd

from ..config import UniverseConfig
from ..resources.db_resource import DBResource

SNAPSHOT_TABLE = "index_membership_snapshot"


@dg.asset(group_name="index_membership")
def index_membership_snapshot(
    context: dg.AssetExecutionContext, universe_tickers: pd.DataFrame, db: DBResource
) -> pd.DataFrame:
    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    df = universe_tickers.copy()
    df["snapshot_date"] = snapshot_date
    engine = db.get_engine()
    df.to_sql(SNAPSHOT_TABLE, engine, schema="stock_data", if_exists="append", index=False)
    context.add_output_metadata({"snapshot_date": snapshot_date, "num_tickers": len(df)})
    return df


@dg.asset(group_name="index_membership")
def index_dropouts(
    context: dg.AssetExecutionContext,
    config: UniverseConfig,
    index_membership_snapshot: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    """Dropouts are computed by diffing the pipeline's own snapshot history —
    there is no free source of true historical Russell/S&P membership changes.
    This means the asset returns empty until enough daily snapshots accumulate
    (see dropout_lookback_days in UniverseConfig)."""
    history = db.read_table_if_exists(SNAPSHOT_TABLE)
    empty = pd.DataFrame(columns=["ticker", "index_name", "last_seen_date"])
    if history.empty:
        context.add_output_metadata({"num_dropouts": 0, "note": "no snapshot history yet"})
        return empty

    history["snapshot_date"] = pd.to_datetime(history["snapshot_date"])
    today = history["snapshot_date"].max()
    cutoff = today - pd.Timedelta(days=config.dropout_lookback_days)
    past_dates = history.loc[history["snapshot_date"] <= cutoff, "snapshot_date"]
    if past_dates.empty:
        context.add_output_metadata({"num_dropouts": 0, "note": "not enough history yet"})
        return empty

    baseline_date = past_dates.max()
    baseline_tickers = set(history.loc[history["snapshot_date"] == baseline_date, "ticker"])
    current_tickers = set(history.loc[history["snapshot_date"] == today, "ticker"])
    dropped = baseline_tickers - current_tickers

    result = (
        history[history["ticker"].isin(dropped) & (history["snapshot_date"] == baseline_date)][
            ["ticker", "index_name"]
        ]
        .drop_duplicates()
        .reset_index(drop=True)
    )
    result["last_seen_date"] = baseline_date.date().isoformat()
    context.add_output_metadata(
        {"num_dropouts": len(result), "baseline_date": baseline_date.date().isoformat()}
    )
    return result
