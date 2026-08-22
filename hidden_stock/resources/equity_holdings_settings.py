"""Shared equity-holdings run settings (one knob for all holdings assets)."""

from __future__ import annotations

import dagster as dg


class EquityHoldingsSettings(dg.ConfigurableResource):
    """Run-level holdings filter — inject into live / history / export / backtest.

    Configure once under ``resources.equity_holdings_settings`` instead of
    repeating per-asset Config blocks.
    """

    ticker_allowlist: list[str] = []
    max_workers: int = 2
    use_llm_fallback: bool = False
    # Primary QoQ depth: calendar years from as_of/today.
    history_lookback_years: int = 5
    # Safety ceiling inside the year window (newest-first).
    history_max_filings: int = 80
