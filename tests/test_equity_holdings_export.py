"""Unit tests for equity holdings CSV/Sheets frame helpers."""

from __future__ import annotations

import pandas as pd

from hidden_stock.quirks.holdings.export import (
    portfolio_by_period_frame,
    positions_qoq_frame,
)


def test_portfolio_by_period_sums_and_drops_exits():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-03-31",
                "investee_ticker": "BILI",
                "investee_name": "Bilibili",
                "action": "hold",
                "market_value_usd": 100.0,
            },
            {
                "period_end": "2024-03-31",
                "investee_ticker": "BILI",
                "investee_name": "Bilibili",
                "action": "hold",
                "market_value_usd": 50.0,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "BILI",
                "investee_name": "Bilibili",
                "action": "exit",
                "market_value_usd": 0.0,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "",
                "investee_name": "Moonshot",
                "action": "hold",
                "market_value_usd": 10.0,
            },
        ]
    )
    out = portfolio_by_period_frame(hist)
    assert list(out.columns) == ["period_end", "investee_ticker", "market_value_usd"]
    q1 = out[out["period_end"] == "2024-03-31"]
    assert len(q1) == 1
    assert float(q1.iloc[0]["market_value_usd"]) == 150.0
    assert "BILI" not in set(out.loc[out["period_end"] == "2024-06-30", "investee_ticker"])
    assert "Moonshot" in set(out["investee_ticker"])


def test_positions_qoq_column_order():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-03-31",
                "investee_ticker": "XPEV",
                "investee_name": "XPeng",
                "cusip": "ABC",
                "action": "buy",
                "shares_held": 1,
                "shares_prev": 0,
                "shares_delta": 1,
                "market_value_usd": 2,
                "value_prev": 0,
                "value_delta": 2,
                "filing_date": "2024-05-01",
                "accession_no": "x",
                "first_seen_period": "2024-03-31",
                "exited_period": None,
                "note": None,
            }
        ]
    )
    out = positions_qoq_frame(hist)
    assert out.iloc[0]["investee_ticker"] == "XPEV"
    assert "action" in out.columns


def test_chart_data_frame_wide_and_excludes_mrdb():
    from hidden_stock.quirks.holdings.export import chart_data_frame

    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-03-31",
                "investee_ticker": "XPEV",
                "investee_name": "XPeng",
                "action": "hold",
                "market_value_usd": 50_000_000,
            },
            {
                "period_end": "2024-03-31",
                "investee_ticker": "MRDB",
                "investee_name": "Meridian",
                "action": "hold",
                "market_value_usd": 2_000_000_000,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "XPEV",
                "investee_name": "XPeng",
                "action": "hold",
                "market_value_usd": 100_000_000,
            },
        ]
    )
    wide = chart_data_frame(hist)
    assert "period_end" in wide.columns
    assert "XPEV" in wide.columns
    assert "MRDB" not in wide.columns
    assert float(wide.loc[wide["period_end"] == "2024-06-30", "XPEV"].iloc[0]) == 100.0
