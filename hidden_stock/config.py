from dagster import Config


class UniverseConfig(Config):
    # sp500 | russell2000 | russell3000 | manual_csv
    index_source: str = "sp500"
    universe_size_cap: int = 500
    dropout_lookback_days: int = 90


class BacktestConfig(Config):
    # Cap the number of unique historical-deletion tickers the backtest
    # chain runs against, for staged validation runs before a full pass.
    ticker_limit: int | None = None
    # Always include these tickers when applying ticker_limit (e.g. SENEA).
    force_tickers: list[str] = ["SENEA"]


# Equity holdings settings live on EquityHoldingsSettings (ConfigurableResource).
# EquityHoldingsConfig kept as a deprecated alias for any leftover launch configs.
class EquityHoldingsConfig(Config):
    """Deprecated: use resources.equity_holdings_settings instead."""

    ticker_allowlist: list[str] = []
    max_workers: int = 2
    use_llm_fallback: bool = False
    history_lookback_years: int = 5
    history_max_filings: int = 80
