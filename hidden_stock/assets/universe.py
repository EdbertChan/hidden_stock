import dagster as dg
import pandas as pd

from ..config import UniverseConfig
from ..resources.universe_resource import UniverseResource


@dg.asset(group_name="universe")
def universe_tickers(
    context: dg.AssetExecutionContext, config: UniverseConfig, universe: UniverseResource
) -> pd.DataFrame:
    df = universe.get_tickers(config.index_source)
    if config.index_source == "sp500" and config.universe_size_cap and len(df) > config.universe_size_cap:
        df = df.head(config.universe_size_cap)
    context.add_output_metadata({"num_tickers": len(df), "index_source": config.index_source})
    return df
