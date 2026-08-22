"""Tests: history pricing + portfolio/chart no fake zeros / no OTC invent."""

import pandas as pd

from hidden_stock.quirks.holdings.export import chart_data_frame, portfolio_by_period_frame
from hidden_stock.quirks.holdings.history import price_history_rows


def test_price_history_rows_does_not_invent_eodhd():
    hist = [
        {
            "period_end": "2024-12-31",
            "investee_ticker": "DIDIY",
            "investee_name": "DiDi Global Inc.",
            "shares_held": 143_911_749.0,
            "ownership_pct": 11.8,
            "market_value_usd": None,
            "action": "new",
            "note": "source=13g",
        }
    ]
    out = price_history_rows(hist)
    assert out[0]["market_value_usd"] is None


def test_price_history_rows_exit_forced_zero():
    hist = [
        {
            "period_end": "2024-12-31",
            "investee_ticker": "FOO",
            "market_value_usd": None,
            "action": "exit",
            "note": "source=13g",
        }
    ]
    out = price_history_rows(hist)
    assert out[0]["market_value_usd"] == 0.0


def test_portfolio_omits_null_market_value_not_zero():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-12-31",
                "investee_ticker": "DIDIY",
                "investee_name": "DiDi",
                "action": "hold",
                "market_value_usd": None,
            },
            {
                "period_end": "2024-12-31",
                "investee_ticker": "GRAB",
                "investee_name": "Grab",
                "action": "hold",
                "market_value_usd": 2e9,
            },
        ]
    )
    port = portfolio_by_period_frame(hist)
    assert list(port["investee_ticker"]) == ["GRAB"]
    assert (port["market_value_usd"] == 0).sum() == 0


def test_chart_includes_table_fv_didiy():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2026-06-30",
                "investee_ticker": "DIDIY",
                "action": "hold",
                "market_value_usd": 1_900_000_000.0,
            },
            {
                "period_end": "2026-06-30",
                "investee_ticker": "GRAB",
                "action": "hold",
                "market_value_usd": 2_020_000_000.0,
            },
            {
                "period_end": "2025-12-31",
                "investee_ticker": "DIDIY",
                "action": "hold",
                "market_value_usd": 3_011_000_000.0,
            },
            {
                "period_end": "2025-12-31",
                "investee_ticker": "GRAB",
                "action": "hold",
                "market_value_usd": 2_674_000_000.0,
            },
        ]
    )
    chart = chart_data_frame(hist, top_n=7)
    assert "DIDIY" in chart.columns
    assert "GRAB" in chart.columns
    # Latest period GRAB $2.02B > DIDIY $1.9B → GRAB first (not all-time max)
    assert list(chart.columns)[1] == "GRAB"
    june = chart.loc[chart["period_end"] == "2026-06-30"].iloc[0]
    assert abs(float(june["DIDIY"]) - 1900.0) < 0.1
