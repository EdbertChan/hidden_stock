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


class EquityHoldingsConfig(Config):
    """Allowlist for smoke / acceptance (BABA, BRK.B, TCEHY). Empty = use price_book_screen."""

    ticker_allowlist: list[str] = []
    max_workers: int = 2
    # Deterministic 13F + note parsers are primary; LLM only if explicitly enabled.
    use_llm_fallback: bool = False
    # QoQ 13F history backtest depth.
    history_max_filings: int = 40
