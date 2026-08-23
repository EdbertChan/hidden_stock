"""Shared equity-holdings run settings (one knob for all holdings assets)."""

from __future__ import annotations

import dagster as dg


class EquityHoldingsSettings(dg.ConfigurableResource):
    """Shared equity-holdings run settings (one knob for all holdings assets).

    Configure once under ``resources.equity_holdings_settings`` instead of
    repeating per-asset Config blocks.

    ``history_lookback_years``: when ``None`` (default), each parent uses
    ``default_lookback_years`` (8 for fanout_13g_hk / TCEHY, else 5). Set an
    int to force the same window for every allowlisted parent.
    """

    ticker_allowlist: list[str] = []
    max_workers: int = 2
    use_llm_fallback: bool = False
    # Primary QoQ depth: calendar years from as_of/today. None = per-parent default.
    history_lookback_years: int | None = None
    # Safety ceiling inside the year window (newest-first).
    history_max_filings: int = 80
